package com.uniscale.app.sync

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** One dose on today's checklist: a stack item in one slot (null slot = as needed). */
data class Dose(
    val name: String,
    val dosage: Double,
    val unit: String,
    val category: String,
    val frequency: String,
    val slot: String?,
    val purpose: String,
    val takenAt: String?
) {
    val taken get() = takenAt != null
}

/**
 * Today's supplement protocol from the hub, checked off on the phone. The hub owns the protocol;
 * the phone only ticks doses. Ticks made offline wait in a queue and are replayed in order; each
 * check-off carries the time it was tapped, so a replay never logs a dose twice.
 */
class SupplementProtocol(context: Context, private val hub: HubSyncManager) {

    private val prefs = context.getSharedPreferences("supplement_protocol", Context.MODE_PRIVATE)
    private val _doses = MutableStateFlow(applyPending(cachedDoses()))
    val doses: StateFlow<List<Dose>> = _doses.asStateFlow()

    /** Sends queued ticks, then fetches today's checklist. False when the hub couldn't be reached. */
    suspend fun refresh(): Boolean = lock.withLock {
        flush()
        val summary = hub.request("/api/supplements?days=1") ?: return@withLock false
        prefs.edit()
            .putString(KEY_CHECKLIST, summary.optJSONArray("checklist")?.toString() ?: "[]")
            .putString(KEY_DAY, LocalDate.now().toString())
            .apply()
        _doses.value = applyPending(cachedDoses())
        true
    }

    suspend fun setTaken(dose: Dose, taken: Boolean) {
        lock.withLock {
            val queue = pending()
            if (taken) {
                queue.put(JSONObject().apply {
                    put("op", "check")
                    put("name", dose.name)
                    put("dosage", dose.dosage)
                    put("unit", dose.unit)
                    put("category", dose.category)
                    put("frequency", dose.frequency)
                    put("slot", dose.slot ?: JSONObject.NULL)
                    put("timestamp", nowUtc())
                })
            } else {
                val at = dose.takenAt ?: return@withLock
                // A tick that never reached the hub is simply dropped from the queue.
                val queuedIndex = (0 until queue.length()).firstOrNull {
                    val op = queue.getJSONObject(it)
                    op.getString("op") == "check" && op.getString("timestamp") == at
                }
                if (queuedIndex != null) {
                    queue.remove(queuedIndex)
                } else {
                    queue.put(JSONObject().put("op", "uncheck").put("name", dose.name).put("timestamp", at))
                }
            }
            savePending(queue)
            _doses.value = applyPending(cachedDoses())
        }
        refresh()
    }

    private suspend fun flush() {
        val queue = pending()
        while (queue.length() > 0) {
            val op = queue.getJSONObject(0)
            val ok = if (op.getString("op") == "check") {
                hub.request("/api/supplements", JSONObject(op.toString()).apply { remove("op") })
            } else {
                hub.request("/api/supplements/intake/delete",
                    JSONObject().put("timestamp", op.getString("timestamp")).put("name", op.getString("name")))
            }
            if (ok == null) break  // offline: keep the rest, in order, for next time
            queue.remove(0)
            savePending(queue)
        }
    }

    // Yesterday's cached checklist still lists today's doses, just with nothing ticked yet.
    private fun cachedDoses(): List<Dose> {
        val fresh = prefs.getString(KEY_DAY, null) == LocalDate.now().toString()
        val arr = JSONArray(prefs.getString(KEY_CHECKLIST, "[]"))
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Dose(
                name = o.optString("name"),
                dosage = o.optDouble("dosage", 0.0),
                unit = o.optString("unit", "mg"),
                category = o.optString("category", "supplement"),
                frequency = o.optString("frequency", "daily"),
                slot = o.optString("slot").takeIf { !o.isNull("slot") && it.isNotBlank() },
                purpose = o.optString("purpose"),
                takenAt = if (fresh && !o.isNull("taken_at")) o.optString("taken_at") else null
            )
        }
    }

    // Shows queued ticks as if the hub already had them.
    private fun applyPending(doses: List<Dose>): List<Dose> {
        var result = doses
        val queue = pending()
        for (i in 0 until queue.length()) {
            val op = queue.getJSONObject(i)
            val ts = op.getString("timestamp")
            result = if (op.getString("op") == "check") {
                val slot = op.optString("slot").takeIf { !op.isNull("slot") }
                val idx = result.indexOfFirst { it.name == op.getString("name") && it.slot == slot && !it.taken }
                if (idx < 0) result else result.toMutableList().also { it[idx] = it[idx].copy(takenAt = ts) }
            } else {
                result.map { if (it.takenAt == ts) it.copy(takenAt = null) else it }
            }
        }
        return result
    }

    private fun pending() = JSONArray(prefs.getString(KEY_PENDING, "[]"))
    private fun savePending(queue: JSONArray) = prefs.edit().putString(KEY_PENDING, queue.toString()).apply()

    companion object {
        private const val KEY_CHECKLIST = "checklist"
        private const val KEY_DAY = "day"
        private const val KEY_PENDING = "pending"
        // One lock for every instance (the app and the reminder job), so the queue is never sent twice at once.
        private val lock = Mutex()

        val SLOTS = listOf("wake", "breakfast", "lunch", "dinner", "bed")

        // The hub stores Python's isoformat ("+00:00"), so send that form: an untick matches it exactly.
        private fun nowUtc(): String = ZonedDateTime.now(ZoneOffset.UTC).truncatedTo(ChronoUnit.SECONDS)
            .format(DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'+00:00'"))
    }
}
