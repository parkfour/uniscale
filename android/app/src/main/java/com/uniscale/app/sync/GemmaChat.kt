package com.uniscale.app.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.uniscale.app.config.AppConfigManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * "Ask Dr. Gemma" from the phone. The conversation lives only on the phone (gemma_chat.json);
 * each question goes to the hub's /api/chat with the whole thread, the same call the dashboard
 * makes. The reply can take minutes, so a WorkManager job waits for it and posts a notification,
 * and the app can be closed meanwhile.
 */
object GemmaChat {
    data class Message(val role: String, val content: String)
    data class State(
        val conversation: Long = System.currentTimeMillis(),
        val messages: List<Message> = emptyList(),
        val waiting: Boolean = false,
        val error: String? = null
    )

    /**
     * The hub's chat assistant, from GET /api/assistant: its name, or null when the hub has none (then
     * the app hides the chat). The last answer is kept, so it holds while the hub is unreachable.
     */
    private val _assistant = MutableStateFlow<String?>(null)
    private var assistantLoaded = false

    fun assistant(context: Context): StateFlow<String?> {
        if (!assistantLoaded) {
            _assistant.value = assistantPrefs(context).getString(KEY_NAME, null)
            assistantLoaded = true
        }
        return _assistant.asStateFlow()
    }

    suspend fun refreshAssistant(context: Context, hub: HubSyncManager) {
        val answer = hub.request("/api/assistant") ?: return
        val name = if (answer.optBoolean("available")) answer.optString("name").ifBlank { "Assistant" } else null
        assistantPrefs(context).edit().putString(KEY_NAME, name).apply()
        assistantLoaded = true
        _assistant.value = name
    }

    private fun assistantPrefs(context: Context) = context.getSharedPreferences("assistant", Context.MODE_PRIVATE)

    /** Set while the chat screen is showing, so a reply doesn't also raise a notification. */
    @Volatile var visible = false

    private val _state = MutableStateFlow<State?>(null)

    fun state(context: Context): StateFlow<State?> {
        if (_state.value == null) _state.value = load(context)
        return _state.asStateFlow()
    }

    fun ask(context: Context, question: String) {
        val current = current(context)
        if (current.waiting || question.isBlank()) return
        save(context, current.copy(messages = current.messages + Message("user", question.trim()), waiting = true, error = null))
        enqueue(context)
    }

    /** Resend the thread after a failed attempt (its last message is still the unanswered question). */
    fun retry(context: Context) {
        val current = current(context)
        if (current.waiting || current.messages.lastOrNull()?.role != "user") return
        save(context, current.copy(waiting = true, error = null))
        enqueue(context)
    }

    fun newChat(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(WORK_NAME)
        save(context, State())
    }

    internal fun current(context: Context) = _state.value ?: load(context)

    private fun enqueue(context: Context) {
        val request = OneTimeWorkRequestBuilder<GemmaWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, request)
    }

    @Synchronized
    internal fun save(context: Context, state: State) {
        _state.value = state
        val json = JSONObject()
            .put("conversation", state.conversation)
            .put("waiting", state.waiting)
            .put("error", state.error ?: JSONObject.NULL)
            .put("messages", JSONArray(state.messages.map { JSONObject().put("role", it.role).put("content", it.content) }))
        file(context).writeText(json.toString())
    }

    private fun load(context: Context): State = runCatching {
        val json = JSONObject(file(context).readText())
        val arr = json.getJSONArray("messages")
        State(
            conversation = json.getLong("conversation"),
            messages = (0 until arr.length()).map { arr.getJSONObject(it).let { m -> Message(m.getString("role"), m.getString("content")) } },
            waiting = json.optBoolean("waiting"),
            error = json.optString("error").takeIf { !json.isNull("error") }
        )
    }.getOrDefault(State())

    private fun file(context: Context) = File(context.filesDir, "gemma_chat.json")

    private const val WORK_NAME = "ask_dr_gemma"
    private const val KEY_NAME = "name"
}

class GemmaWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val asked = GemmaChat.current(applicationContext)
        if (!asked.waiting) return Result.success()
        val hub = HubSyncManager(applicationContext, AppConfigManager(applicationContext))
        val body = JSONObject().put("messages", JSONArray(asked.messages.map {
            JSONObject().put("role", it.role).put("content", it.content)
        }))
        // The hub gives the model up to 4 minutes; wait a little longer than that.
        val response = hub.request("/api/chat", body, readTimeoutSeconds = REPLY_TIMEOUT_SECONDS)

        val now = GemmaChat.current(applicationContext)
        if (now.conversation != asked.conversation) return Result.success()  // "New chat" was tapped meanwhile
        if (response == null) {
            GemmaChat.save(applicationContext, now.copy(waiting = false, error = "Couldn't reach Dr. Gemma. Tap Retry."))
            return Result.success()
        }
        var reply = response.optString("reply").ifBlank { "(No reply.)" }
        if (response.optBoolean("truncated")) reply += "\n\n(The reply was cut off. Ask Dr. Gemma to continue.)"
        GemmaChat.save(applicationContext, now.copy(messages = now.messages + GemmaChat.Message("assistant", reply), waiting = false))
        if (!GemmaChat.visible) {
            val name = GemmaChat.assistant(applicationContext).value ?: "The assistant"
            Notifications.assistantReplied(applicationContext, name, reply.take(NOTIFICATION_CHARS))
        }
        return Result.success()
    }

    private companion object {
        const val REPLY_TIMEOUT_SECONDS = 300L
        const val NOTIFICATION_CHARS = 300
    }
}
