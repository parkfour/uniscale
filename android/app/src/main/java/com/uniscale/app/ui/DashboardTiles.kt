package com.uniscale.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Bedtime
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.DirectionsRun
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.filled.MonitorWeight
import androidx.compose.material.icons.filled.Place
import androidx.compose.material.icons.filled.Remove
import androidx.compose.material.icons.filled.Speed
import androidx.compose.material.icons.filled.SwapHoriz
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.uniscale.app.model.BloodPressureData
import com.uniscale.app.model.CounterState
import com.uniscale.app.model.DwellPlace
import com.uniscale.app.model.EcgData
import com.uniscale.app.model.SleepData
import com.uniscale.app.model.WatchActivityData
import com.uniscale.app.model.WeightData
import com.uniscale.app.sync.Dose
import com.uniscale.app.sync.SupplementProtocol
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** Which tile of the dashboard is expanded to the full row (at most one per row). */
enum class Tile { WATCH, SLEEP, WEIGHT, BP, ECG, COUNTER, PLACES }

/**
 * Two half-width tiles side by side; when one is expanded it takes the whole row and the other
 * is hidden. Tapping a half tile expands it, tapping the expanded one shrinks it back.
 */
@Composable
fun TileRow(
    left: Tile,
    right: Tile,
    expanded: Tile?,
    onToggle: (Tile) -> Unit,
    half: @Composable (Tile, Modifier) -> Unit,
    full: @Composable (Tile, Modifier) -> Unit
) {
    when (expanded) {
        left, right -> full(expanded, Modifier.clickable { onToggle(expanded) })
        else -> Row(
            modifier = Modifier.fillMaxWidth().height(IntrinsicSize.Min),
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            half(left, Modifier.weight(1f).fillMaxHeight().clickable { onToggle(left) })
            half(right, Modifier.weight(1f).fillMaxHeight().clickable { onToggle(right) })
        }
    }
}

/** A half-width tile: title, one big value, and a few detail lines under it. */
@Composable
fun HalfTile(
    title: String,
    icon: ImageVector,
    tint: Color,
    big: String?,
    details: List<String>,
    empty: String,
    modifier: Modifier = Modifier
) {
    ElevatedCard(modifier = modifier, shape = RoundedCornerShape(20.dp)) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, contentDescription = null, tint = tint, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text(title, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Spacer(modifier = Modifier.height(10.dp))
            if (big == null) {
                Text(empty, fontSize = 13.sp, color = MaterialTheme.colorScheme.outline)
            } else {
                Text(big, fontSize = 30.sp, fontWeight = FontWeight.Bold, maxLines = 1)
                details.forEach {
                    Text(it, fontSize = 12.sp, color = MaterialTheme.colorScheme.outline, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
        }
    }
}

private val timeFormat = DateTimeFormatter.ofPattern("MMM d, HH:mm").withZone(ZoneId.systemDefault())

private fun Float.trim1() = if (this % 1f == 0f) toInt().toString() else "%.1f".format(this)

@Composable
fun WatchHalf(a: WatchActivityData?, modifier: Modifier) = HalfTile(
    "Activity", Icons.Default.DirectionsRun, Color(0xFFF59E0B),
    big = a?.takeIf { it.steps > 0 }?.let { "%,d".format(it.steps) },
    details = listOfNotNull("steps today", a?.let { "${it.distanceKm} km" }),
    empty = "No steps yet today",
    modifier = modifier
)

@Composable
fun SleepHalf(s: SleepData?, modifier: Modifier) = HalfTile(
    "Sleep", Icons.Default.Bedtime, Color(0xFF8B5CF6),
    big = s?.takeIf { it.durationSeconds > 0 }?.let { "${it.durationHours} h" },
    details = listOfNotNull(
        s?.let { sl -> (sl.deviceSleepScore?.let { "Score $it" } ?: sl.estimatedSleepScore?.let { "Score ~$it" }) },
        s?.let { "Deep ${it.deepSleepMinutes}m · REM ${it.remSleepMinutes}m" },
        s?.hrvRmssdMs?.let { "HRV $it ms" }
    ),
    empty = "No sleep yet",
    modifier = modifier
)

@Composable
fun WeightHalf(w: WeightData?, unit: String, modifier: Modifier) = HalfTile(
    "Weight", Icons.Default.MonitorWeight, MaterialTheme.colorScheme.primary,
    big = w?.let { if (unit == "kg") "${it.weightKg.trim1()} kg" else "${it.weightLb.trim1()} lb" },
    details = listOfNotNull(
        w?.bodyFatPercent?.let { "Body fat ${it.trim1()}%" },
        w?.bmi?.let { "BMI ${it.trim1()}" },
        w?.let { timeFormat.format(it.timestamp) }
    ),
    empty = "Step on the scale",
    modifier = modifier
)

@Composable
fun BpHalf(bp: BloodPressureData?, measuring: String?, modifier: Modifier) = HalfTile(
    "Blood Pressure", Icons.Default.Speed, Color(0xFFEF4444),
    big = bp?.let { "${it.systolic}/${it.diastolic}" },
    details = listOfNotNull(
        measuring,
        bp?.pulse?.takeIf { it > 0 }?.let { "Pulse $it bpm" },  // 0 = not recorded
        bp?.let { timeFormat.format(it.timestamp) }
    ),
    empty = measuring ?: "Start the cuff",
    modifier = modifier
)

@Composable
fun EcgHalf(ecg: EcgData?, modifier: Modifier) = HalfTile(
    "ECG", Icons.Default.Favorite, Color(0xFF38BDF8),
    big = ecg?.let { if (it.heartRate > 0) "${it.heartRate} bpm" else "-- bpm" },
    details = listOfNotNull(ecg?.rhythmStatus, ecg?.let { timeFormat.format(it.timestamp) }),
    empty = "Hold the BP2's side electrodes",
    modifier = modifier
)

/** Protocol summary: doses taken today, and what's still open in the earliest slot that has any. */
@Composable
fun ProtocolHalf(doses: List<Dose>, modifier: Modifier) {
    val next = doses.filter { !it.taken }.groupBy { it.slot }.entries.firstOrNull()
    HalfTile(
        "Protocol", Icons.Default.CheckCircle, Color(0xFFF59E0B),
        big = if (doses.isEmpty()) null else "${doses.count { it.taken }}/${doses.size}",
        details = if (next == null) listOf("All done today") else listOf(
            (next.key?.replaceFirstChar { it.uppercase() } ?: "As needed") + ": ${next.value.size} left",
            next.value.joinToString(", ") { it.name }
        ),
        empty = "No protocol on the hub",
        modifier = modifier
    )
}

private val COUNTER_COLORS = listOf(
    Color(0xFF38BDF8), Color(0xFF10B981), Color(0xFFF59E0B), Color(0xFFA78BFA),
    Color(0xFFF472B6), Color(0xFFF87171), Color(0xFF2DD4BF), Color(0xFFFACC15)
)

private fun counterColor(i: Int) = COUNTER_COLORS[i % COUNTER_COLORS.size]

@Composable
fun CounterHalf(counter: CounterState, modifier: Modifier = Modifier) = HalfTile(
    counter.title ?: "Counter", Icons.Default.SwapHoriz, Color(0xFF0EA5E9),
    big = counter.elements.takeIf { it.isNotEmpty() }?.joinToString(" / ") { "${counter.counts[it.key] ?: 0}" },
    details = counter.elements.map { "${it.label}: ${counter.counts[it.key] ?: 0}" } + "Tap to adjust",
    empty = "Set up a counter in your profile on the hub",
    modifier = modifier
)

@Composable
fun PlacesHalf(places: List<DwellPlace>, modifier: Modifier = Modifier) {
    val active = places.filter { !it.isDeleted }
    HalfTile(
        "Places (>1h)", Icons.Default.Place, Color(0xFF6366F1),
        big = if (active.isEmpty()) null else "${active.size} Places",
        details = if (active.isEmpty()) emptyList() else active.take(2).map { p ->
            val label = p.name ?: "${"%.2f".format(p.avgLat)}, ${"%.2f".format(p.avgLon)}"
            "$label · ${p.dwellFormatted} · ${p.date.takeLast(5)}"
        },
        empty = "No places yet",
        modifier = modifier
    )
}

/** The protocol checklist as its own page, since it can be longer than the screen. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProtocolScreen(doses: List<Dose>, onToggle: (Dose, Boolean) -> Unit, onDone: () -> Unit) {
    BackHandler(onBack = onDone)
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Today's Protocol") },
                actions = { TextButton(onClick = onDone) { Text("Done", fontWeight = FontWeight.SemiBold) } }
            )
        }
    ) { padding ->
        LazyColumn(modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp)) {
            item { ProtocolCard(doses = doses, onToggle = onToggle) }
            item { Spacer(modifier = Modifier.navigationBarsPadding().height(16.dp)) }
        }
    }
}

@Composable
fun WatchFull(watchActivity: WatchActivityData?, modifier: Modifier = Modifier) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.DirectionsRun, contentDescription = null, tint = Color(0xFFF59E0B))
                Spacer(modifier = Modifier.width(8.dp))
                Text("Watch Activity", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                Badge(containerColor = Color(0xFF10B981).copy(alpha = 0.2f)) {
                    Text("Local Sync", color = Color(0xFF10B981), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (watchActivity != null && watchActivity.steps > 0) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceAround,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "${watchActivity.steps}",
                            fontSize = 32.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onSurface
                        )
                        Text("Steps Today", fontSize = 13.sp, color = MaterialTheme.colorScheme.outline)
                    }
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "${watchActivity.distanceKm} km",
                            fontSize = 32.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.primary
                        )
                        Text("Distance", fontSize = 13.sp, color = MaterialTheme.colorScheme.outline)
                    }
                }
            } else {
                Text(
                    "Connected via Gadgetbridge. Daily steps & distance sync automatically.",
                    color = MaterialTheme.colorScheme.outline,
                    fontSize = 14.sp
                )
            }
        }
    }
}

@Composable
fun SleepFull(latestSleep: SleepData?, modifier: Modifier = Modifier) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Bedtime, contentDescription = null, tint = Color(0xFF8B5CF6))
                Spacer(modifier = Modifier.width(8.dp))
                Text("Sleep & Recovery", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                if (latestSleep?.deviceSleepScore != null || latestSleep?.estimatedSleepScore != null) {
                    Badge(containerColor = Color(0xFF8B5CF6).copy(alpha = 0.2f)) {
                        Text(
                            latestSleep.deviceSleepScore?.let { "$it/100 Score" } ?: "~${latestSleep.estimatedSleepScore}/100 Est.",
                            color = Color(0xFF8B5CF6),
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (latestSleep != null && latestSleep.durationSeconds > 0) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceAround,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "${latestSleep.durationHours} hrs",
                            fontSize = 28.sp,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onSurface
                        )
                        Text(latestSleep.date, fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                    }
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "${latestSleep.deepSleepMinutes}m",
                            fontSize = 24.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF8B5CF6)
                        )
                        Text("Deep Sleep", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                    }
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(
                            text = "${latestSleep.remSleepMinutes}m",
                            fontSize = 24.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF06B6D4)
                        )
                        Text("REM Sleep", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                    }
                }

                Spacer(modifier = Modifier.height(12.dp))

                val deepM = latestSleep.deepSleepMinutes
                val remM = latestSleep.remSleepMinutes
                val lightM = latestSleep.lightSleepMinutes
                val totM = (deepM + remM + lightM).coerceAtLeast(1L)
                val deepWeight = deepM.toFloat() / totM
                val remWeight = remM.toFloat() / totM
                val lightWeight = lightM.toFloat() / totM

                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(8.dp)
                        .background(Color(0xFF334155), RoundedCornerShape(4.dp))
                ) {
                    if (deepWeight > 0f) {
                        Box(
                            modifier = Modifier
                                .weight(deepWeight)
                                .fillMaxHeight()
                                .background(Color(0xFF8B5CF6), RoundedCornerShape(topStart = 4.dp, bottomStart = 4.dp))
                        )
                    }
                    if (remWeight > 0f) {
                        Box(
                            modifier = Modifier
                                .weight(remWeight)
                                .fillMaxHeight()
                                .background(Color(0xFF06B6D4))
                        )
                    }
                    if (lightWeight > 0f) {
                        Box(
                            modifier = Modifier
                                .weight(lightWeight)
                                .fillMaxHeight()
                                .background(Color(0xFF38BDF8), RoundedCornerShape(topEnd = 4.dp, bottomEnd = 4.dp))
                        )
                    }
                }

                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    "Deep: ${deepM}m • REM: ${remM}m • Light: ${lightM}m" + if (latestSleep.hrvRmssdMs != null) " • HRV: ${latestSleep.hrvRmssdMs}ms" else "",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            } else {
                Text(
                    "No sleep yet. Turn on Gadgetbridge → Settings → Auto export, then in Settings → Data tap Watch and pick that file.",
                    color = MaterialTheme.colorScheme.outline,
                    fontSize = 14.sp
                )
            }
        }
    }
}

/** "+0.2", "−3", "±0": sign always shown, real minus sign. */
internal fun signed(delta: Float, decimals: Int): String {
    val r = "%.${decimals}f".format(kotlin.math.abs(delta))
    val zero = r.all { it == '0' || it == '.' }
    return when {
        zero -> "±$r"
        delta > 0 -> "+$r"
        else -> "−$r"
    }
}

@Composable
private fun ChangeSinceText(change: String, since: java.time.Instant) {
    Text(
        "$change since ${DateTimeFormatter.ofPattern("MMM d").withZone(ZoneId.systemDefault()).format(since)}",
        fontSize = 14.sp,
        color = MaterialTheme.colorScheme.outline,
        modifier = Modifier.padding(top = 2.dp)
    )
}

@Composable
fun WeightFull(weight: WeightData?, modifier: Modifier = Modifier, previous: WeightData? = null) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.MonitorWeight, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                Spacer(modifier = Modifier.width(8.dp))
                Text("Weight & Body Composition", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                if (weight != null) {
                    Text(weight.deviceName, fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (weight != null) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(
                        text = "${weight.weightLb}",
                        fontSize = 44.sp,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onSurface
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text("lb", fontSize = 20.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(bottom = 6.dp))
                    Spacer(modifier = Modifier.width(16.dp))
                    Text(
                        text = "(${weight.weightKg} kg)",
                        fontSize = 16.sp,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(bottom = 6.dp)
                    )
                }
                if (previous != null) {
                    ChangeSinceText("${signed(weight.weightLb - previous.weightLb, 1)} lb", previous.timestamp)
                }
                if (weight.bodyFatPercent != null) {
                    Spacer(modifier = Modifier.height(14.dp))
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            BiaStatBadge(label = "Body Fat", value = "${weight.bodyFatPercent}%", color = Color(0xFF8B5CF6))
                            BiaStatBadge(label = "Muscle", value = "${weight.muscleMassLb ?: "--"} lb", color = Color(0xFF10B981))
                            BiaStatBadge(label = "Water", value = "${weight.waterPercent ?: "--"}%", color = Color(0xFF38BDF8))
                            BiaStatBadge(label = "BMI", value = "${weight.bmi ?: "--"}", color = Color(0xFFF59E0B))
                        }
                        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            BiaStatBadge(label = "Bone", value = "${weight.boneMassLb ?: "--"} lb", color = Color(0xFF64748B))
                            BiaStatBadge(label = "Visceral", value = "${weight.visceralFat ?: "--"}", color = Color(0xFFEF4444))
                            BiaStatBadge(label = "BMR", value = "${weight.bmrKcal ?: "--"} kcal", color = Color(0xFFEC4899))
                            BiaStatBadge(label = "Impedance", value = "${weight.impedance ?: "--"} Ω", color = MaterialTheme.colorScheme.primary)
                        }
                    }
                } else if (weight.impedance != null) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("Impedance: ${weight.impedance} Ω", fontSize = 13.sp, color = MaterialTheme.colorScheme.outline)
                }
            } else {
                Text(
                    "Step on your Yunmai, Fitdays, or VeSync scale to measure...",
                    color = MaterialTheme.colorScheme.outline,
                    fontSize = 14.sp
                )
            }
        }
    }
}

@Composable
fun BpFull(bp: BloodPressureData?, modifier: Modifier = Modifier, previous: BloodPressureData? = null) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Speed, contentDescription = null, tint = Color(0xFFEF4444))
                Spacer(modifier = Modifier.width(8.dp))
                Text("Blood Pressure", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                if (bp != null) {
                    Text(bp.deviceName, fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (bp != null) {
                Row(verticalAlignment = Alignment.Bottom) {
                    Text(
                        text = "${bp.systolic}/${bp.diastolic}",
                        fontSize = 42.sp,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onSurface
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text("mmHg", fontSize = 18.sp, modifier = Modifier.padding(bottom = 6.dp))
                    Spacer(modifier = Modifier.width(20.dp))
                    if (bp.pulse > 0) Text(  // 0 = not recorded
                        text = "Pulse: ${bp.pulse} bpm",
                        fontSize = 16.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(bottom = 6.dp)
                    )
                }
                if (previous != null) {
                    ChangeSinceText(
                        "${signed((bp.systolic - previous.systolic).toFloat(), 0)}/" +
                            "${signed((bp.diastolic - previous.diastolic).toFloat(), 0)} mmHg",
                        previous.timestamp
                    )
                }
            } else {
                Text(
                    "Press start on your Viatom BP2 or Omron cuff...",
                    color = MaterialTheme.colorScheme.outline,
                    fontSize = 14.sp
                )
            }
        }
    }
}

@Composable
fun EcgFull(ecg: EcgData?, tall: Boolean, modifier: Modifier = Modifier) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Favorite, contentDescription = null, tint = Color(0xFF38BDF8))
                Spacer(modifier = Modifier.width(8.dp))
                Text("Lead-I ECG (Viatom BP2)", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                if (ecg != null) {
                    Text(if (ecg.heartRate > 0) "${ecg.heartRate} bpm" else "-- bpm", fontWeight = FontWeight.Bold, color = Color(0xFF38BDF8))
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            val samples = ecg?.samplesMv ?: emptyList()
            EcgWaveformCanvas(
            samplesMv = samples,
            modifier = Modifier.fillMaxWidth().height(if (tall) 280.dp else 140.dp)
                .clip(RoundedCornerShape(12.dp)).background(Color(0xFF0F172A))
        )

            Spacer(modifier = Modifier.height(8.dp))
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(
                    text = ecg?.rhythmStatus ?: "Hold side electrodes on BP2 to stream rhythm",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.outline
                )
                if (ecg != null) {
                    Text("125 Hz Lead-I", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                }
            }
        }
    }
}

@Composable
fun CounterFull(
    counter: CounterState,
    onAdjust: (key: String, delta: Int) -> Unit,
    modifier: Modifier = Modifier
) {
    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.SwapHoriz, contentDescription = null, tint = Color(0xFF0EA5E9))
                Spacer(modifier = Modifier.width(8.dp))
                Text(counter.title ?: "Counter", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                Badge(containerColor = Color(0xFF0EA5E9).copy(alpha = 0.2f)) {
                    Text("Daily", color = Color(0xFF0EA5E9), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                }
            }

            if (counter.elements.isEmpty()) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(
                    "No counter set up. Add one in your profile on the hub: a title and its buttons, e.g. D Tracker with D(out), D(in).",
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.outline
                )
                return@Column
            }

            Spacer(modifier = Modifier.height(12.dp))
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                counter.elements.forEachIndexed { i, e ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f), RoundedCornerShape(14.dp))
                            .padding(horizontal = 14.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(e.label, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = counterColor(i), modifier = Modifier.weight(1f))
                        FilledTonalIconButton(onClick = { onAdjust(e.key, -1) }, modifier = Modifier.size(40.dp)) {
                            Icon(Icons.Default.Remove, contentDescription = "Take one from ${e.label}")
                        }
                        Text(
                            "${counter.counts[e.key] ?: 0}",
                            fontSize = 28.sp,
                            fontWeight = FontWeight.ExtraBold,
                            textAlign = TextAlign.Center,
                            modifier = Modifier.width(64.dp)
                        )
                        FilledTonalIconButton(onClick = { onAdjust(e.key, 1) }, modifier = Modifier.size(40.dp)) {
                            Icon(Icons.Default.Add, contentDescription = "Add one to ${e.label}")
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(12.dp))
            Text(
                "All-time: " + counter.elements.joinToString(" · ") { "${it.label} ${counter.totals[it.key] ?: 0}" },
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.outline
            )

            val recent = counter.dailyHistory.take(7)
            if (recent.isNotEmpty()) {
                Spacer(modifier = Modifier.height(16.dp))
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                Spacer(modifier = Modifier.height(12.dp))
                Text(
                    "Recent Daily History",
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.primary
                )
                Spacer(modifier = Modifier.height(8.dp))
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    recent.forEach { rec ->
                        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text(rec.date, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                            counter.elements.forEachIndexed { i, e ->
                                Text(
                                    "${e.label}: ${rec.counts[e.key] ?: 0}",
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = counterColor(i),
                                    modifier = Modifier.padding(end = 10.dp)
                                )
                            }
                            Text("Total: ${rec.total}", fontSize = 12.sp, color = MaterialTheme.colorScheme.outline)
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun PlacesFull(
    places: List<DwellPlace>,
    onAddPoi: (DwellPlace, String) -> Unit,
    onDeletePlace: (DwellPlace) -> Unit,
    onLookupPois: (DwellPlace) -> Unit,
    onRefresh: () -> Unit,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    var editingPlaceId by remember { mutableStateOf<String?>(null) }
    var customPoiText by remember { mutableStateOf("") }

    ElevatedCard(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(20.dp)
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Place, contentDescription = null, tint = Color(0xFF6366F1))
                Spacer(modifier = Modifier.width(8.dp))
                Text("Places (>1 hr)", fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
                Spacer(modifier = Modifier.weight(1f))
                Badge(containerColor = Color(0xFF6366F1).copy(alpha = 0.2f)) {
                    Text("OwnTracks", color = Color(0xFF818CF8), fontSize = 11.sp, fontWeight = FontWeight.Bold)
                }
            }

            Spacer(modifier = Modifier.height(14.dp))

            val activePlaces = places.filter { !it.isDeleted }
            if (activePlaces.isEmpty()) {
                Text(
                    "No places yet. They come from OwnTracks location reports (an hour or more in one spot) or from Add Place on the web dashboard.",
                    color = MaterialTheme.colorScheme.outline,
                    fontSize = 14.sp
                )
                Spacer(modifier = Modifier.height(8.dp))
                TextButton(onClick = onRefresh) {
                    Text("Refresh Places")
                }
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    activePlaces.forEach { place ->
                        ElevatedCard(
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(14.dp),
                            colors = CardDefaults.elevatedCardColors(
                                containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f)
                            )
                        ) {
                            Column(modifier = Modifier.padding(14.dp)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text(
                                            text = place.name ?: "Unlabeled Place",
                                            fontWeight = FontWeight.Bold,
                                            fontSize = 15.sp,
                                            color = if (place.name != null) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.outline
                                        )
                                        val hasCoords = place.avgLat != 0.0 || place.avgLon != 0.0
                                        Text(
                                            text = "${place.date} · ${place.dwellFormatted}" +
                                                if (hasCoords) " (${"%.4f".format(place.avgLat)}, ${"%.4f".format(place.avgLon)} ± ${place.confidenceRange.toInt()}m)" else "",
                                            fontSize = 12.sp,
                                            color = MaterialTheme.colorScheme.outline
                                        )
                                    }
                                    IconButton(onClick = { onDeletePlace(place) }) {
                                        Icon(
                                            Icons.Default.Delete,
                                            contentDescription = "Delete Place",
                                            tint = MaterialTheme.colorScheme.error.copy(alpha = 0.7f),
                                            modifier = Modifier.size(20.dp)
                                        )
                                    }
                                }

                                // Suggested POIs from web service
                                if (place.candidatePois.isNotEmpty()) {
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Text(
                                        "Nearby POIs (tap to assign):",
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.SemiBold,
                                        color = MaterialTheme.colorScheme.outline
                                    )
                                    Spacer(modifier = Modifier.height(4.dp))
                                    FlowRow(
                                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                                        verticalArrangement = Arrangement.spacedBy(4.dp)
                                    ) {
                                        place.candidatePois.take(6).forEach { poi ->
                                            SuggestionChip(
                                                onClick = { onAddPoi(place, poi) },
                                                label = { Text(poi, fontSize = 12.sp) },
                                                colors = SuggestionChipDefaults.suggestionChipColors(
                                                    containerColor = if (place.name == poi) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface
                                                )
                                            )
                                        }
                                    }
                                }

                                Spacer(modifier = Modifier.height(8.dp))
                                if (editingPlaceId == place.id) {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        OutlinedTextField(
                                            value = customPoiText,
                                            onValueChange = { customPoiText = it },
                                            placeholder = { Text("Enter POI name") },
                                            modifier = Modifier.weight(1f),
                                            singleLine = true
                                        )
                                        Spacer(modifier = Modifier.width(6.dp))
                                        IconButton(onClick = {
                                            if (customPoiText.isNotBlank()) {
                                                onAddPoi(place, customPoiText.trim())
                                                customPoiText = ""
                                                editingPlaceId = null
                                            }
                                        }) {
                                            Icon(Icons.Default.Check, contentDescription = "Save POI")
                                        }
                                    }
                                } else {
                                    Row(
                                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        TextButton(
                                            onClick = {
                                                customPoiText = place.name.orEmpty()
                                                editingPlaceId = place.id
                                            },
                                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp)
                                        ) {
                                            Text(if (place.name == null) "+ Add POI" else "Edit POI", fontSize = 12.sp)
                                        }

                                        if (place.avgLat != 0.0 || place.avgLon != 0.0) {
                                            TextButton(
                                                onClick = { openMap(context, place) },
                                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp)
                                            ) {
                                                Text("Map", fontSize = 12.sp)
                                            }
                                        }
                                        TextButton(
                                            onClick = { onLookupPois(place) },
                                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp)
                                        ) {
                                            Text("Lookup POIs", fontSize = 12.sp)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** Opens the place in the phone's maps app (geo: intent), with a pin labelled by its name. */
private fun openMap(context: android.content.Context, place: DwellPlace) {
    val label = android.net.Uri.encode(place.name ?: "Place")
    val uri = android.net.Uri.parse("geo:${place.avgLat},${place.avgLon}?q=${place.avgLat},${place.avgLon}($label)")
    try {
        context.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, uri))
    } catch (e: android.content.ActivityNotFoundException) {
        android.widget.Toast.makeText(context, "No maps app installed", android.widget.Toast.LENGTH_SHORT).show()
    }
}
