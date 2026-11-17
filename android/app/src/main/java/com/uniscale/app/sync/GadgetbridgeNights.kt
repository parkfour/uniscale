package com.uniscale.app.sync

import com.uniscale.app.model.SleepData
import java.time.Instant
import java.time.ZoneId
import kotlin.math.abs
import kotlin.math.roundToInt

/** A Garmin sleep event from Gadgetbridge's GARMIN_EVENT_SAMPLE (EVENT = 74): type 0 = start, 1 = stop. */
data class GbSleepEvent(val timeMs: Long, val type: Int)

/** One row of GARMIN_HRV_SUMMARY_SAMPLE. */
data class GbHrvSummary(val timeMs: Long, val lastNightAverage: Int?, val statusNum: Int?)

/**
 * Turns rows from Gadgetbridge's database into one SleepData per night. Pure, so it is unit-tested
 * without Android; GadgetbridgeExport does the reading.
 */
object GadgetbridgeNights {
    const val DEVICE_NAME = "Garmin (Gadgetbridge)" // same as the server's FIT import, so the two dedupe
    const val DEVICE_ADDRESS = "gadgetbridge"

    // Gadgetbridge stores Garmin's FIT sleep levels; checked against the FIT file for the same night.
    private const val AWAKE = 1
    private const val LIGHT = 2
    private const val DEEP = 3
    private const val REM = 4
    private val HRV_STATUS = mapOf(1 to "poor", 2 to "low", 3 to "unbalanced", 4 to "balanced")

    // The watch writes the score at wake-up and the HRV summary up to ~1 h later.
    private const val AFTER_WAKE_MS = 3 * 3_600_000L

    fun build(
        events: List<GbSleepEvent>,
        stages: List<Pair<Long, Int>>,
        scores: List<Pair<Long, Int>>,
        hrv: List<GbHrvSummary>,
        spo2: List<Pair<Long, Int>>,
        respiration: List<Pair<Long, Float>>,
        zone: ZoneId
    ): List<SleepData> {
        val nights = mutableListOf<SleepData>()
        var start: Long? = null
        for (e in events.sortedBy { it.timeMs }) {
            when {
                e.type == 0 -> start = e.timeMs
                e.type == 1 && start != null && e.timeMs > start -> {
                    nights += night(start, e.timeMs, stages, scores, hrv, spo2, respiration, zone)
                    start = null
                }
            }
        }
        return nights.sortedByDescending { it.timestamp }
    }

    private fun night(
        start: Long,
        stop: Long,
        stages: List<Pair<Long, Int>>,
        scores: List<Pair<Long, Int>>,
        hrv: List<GbHrvSummary>,
        spo2: List<Pair<Long, Int>>,
        respiration: List<Pair<Long, Float>>,
        zone: ZoneId
    ): SleepData {
        // Each stage lasts until the next one; the last one until the stop event.
        val inNight = stages.filter { it.first in start until stop }.sortedBy { it.first }
        val seconds = mutableMapOf<Int, Long>()
        inNight.forEachIndexed { i, (t, level) ->
            val end = if (i + 1 < inNight.size) inNight[i + 1].first else stop
            seconds[level] = (seconds[level] ?: 0L) + (end - t) / 1000
        }
        val afterWake = start..(stop + AFTER_WAKE_MS)
        val score = scores.filter { it.first in afterWake }.minByOrNull { abs(it.first - stop) }?.second
        val hrvRow = hrv.filter { it.timeMs in afterWake }.minByOrNull { abs(it.timeMs - stop) }
        val o2 = spo2.filter { it.first in start..stop && it.second > 0 }.map { it.second }
        val breaths = respiration.filter { it.first in start..stop && it.second > 0f }.map { it.second }

        return SleepData(
            date = Instant.ofEpochMilli(stop).atZone(zone).toLocalDate().toString(),
            timestamp = Instant.ofEpochMilli(stop),
            durationSeconds = (stop - start) / 1000,
            deepSleepSeconds = seconds[DEEP] ?: 0,
            remSleepSeconds = seconds[REM] ?: 0,
            lightSleepSeconds = seconds[LIGHT] ?: 0,
            awakeSeconds = seconds[AWAKE] ?: 0,
            deviceSleepScore = score,
            hrvRmssdMs = hrvRow?.lastNightAverage?.toFloat(),
            hrvStatus = hrvRow?.statusNum?.let { HRV_STATUS[it] },
            avgSpo2 = o2.takeIf { it.isNotEmpty() }?.average()?.let { round1(it) },
            lowestSpo2 = o2.minOrNull()?.toFloat(),
            avgRespiration = breaths.takeIf { it.isNotEmpty() }?.average()?.let { round1(it) },
            deviceName = DEVICE_NAME,
            deviceAddress = DEVICE_ADDRESS
        )
    }

    private fun round1(v: Double) = (v * 10).roundToInt() / 10f
}
