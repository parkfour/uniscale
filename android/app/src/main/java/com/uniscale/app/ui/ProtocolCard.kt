package com.uniscale.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.uniscale.app.sync.Dose
import com.uniscale.app.sync.SupplementProtocol

/** Today's supplement protocol from the hub, grouped by slot in day order; tap a row to tick it off. */
@Composable
fun ProtocolCard(doses: List<Dose>, onToggle: (Dose, Boolean) -> Unit) {
    val groups = doses.groupBy { it.slot }
    val order = SupplementProtocol.SLOTS + listOf<String?>(null)
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Today's Protocol", fontWeight = FontWeight.SemiBold, fontSize = 18.sp)
                Text(
                    "${doses.count { it.taken }}/${doses.size}",
                    fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.outline
                )
            }
            order.filter { it in groups }.forEach { slot ->
                Text(
                    slot?.replaceFirstChar { it.uppercase() } ?: "As needed",
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 10.dp, bottom = 2.dp)
                )
                groups.getValue(slot).forEach { dose ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable { onToggle(dose, !dose.taken) },
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Checkbox(checked = dose.taken, onCheckedChange = { onToggle(dose, it) })
                        Column {
                            Text(
                                dose.name,
                                fontSize = 15.sp,
                                textDecoration = if (dose.taken) TextDecoration.LineThrough else null
                            )
                            Text(
                                "${dose.dosage.let { if (it % 1.0 == 0.0) it.toLong().toString() else it.toString() }} ${dose.unit}",
                                fontSize = 12.sp,
                                color = MaterialTheme.colorScheme.outline
                            )
                        }
                    }
                }
            }
        }
    }
}
