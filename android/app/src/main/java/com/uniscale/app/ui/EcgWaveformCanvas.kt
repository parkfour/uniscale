package com.uniscale.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp

@Composable
fun EcgWaveformCanvas(
    samplesMv: List<Float>,
    modifier: Modifier = Modifier
        .fillMaxWidth()
        .height(140.dp)
        .clip(RoundedCornerShape(12.dp))
        .background(Color(0xFF0F172A))
) {
    Canvas(modifier = modifier) {
        val width = size.width
        val height = size.height
        val centerY = height / 2f

        // Draw classic ECG background grid lines
        val gridStep = 20f
        var x = 0f
        while (x < width) {
            drawLine(
                color = Color(0xFF1E293B),
                start = Offset(x, 0f),
                end = Offset(x, height),
                strokeWidth = 1f
            )
            x += gridStep
        }

        var y = 0f
        while (y < height) {
            drawLine(
                color = Color(0xFF1E293B),
                start = Offset(0f, y),
                end = Offset(width, y),
                strokeWidth = 1f
            )
            y += gridStep
        }

        if (samplesMv.isEmpty()) {
            // Draw baseline if no samples
            drawLine(
                color = Color(0xFF38BDF8),
                start = Offset(0f, centerY),
                end = Offset(width, centerY),
                strokeWidth = 2f
            )
            return@Canvas
        }

        // Draw ECG waveform path
        val path = Path()
        val visibleCount = minOf(samplesMv.size, 375) // 3 seconds of rhythm at 125 Hz
        val startIdx = samplesMv.size - visibleCount
        val stepX = width / maxOf(1, visibleCount - 1).toFloat()

        // Compute baseline DC offset so the rhythm is centered vertically on the canvas
        var sum = 0f
        for (i in 0 until visibleCount) {
            sum += samplesMv[startIdx + i]
        }
        val baseline = sum / maxOf(1, visibleCount).toFloat()
        val scaleY = 75f

        val firstY = centerY - ((samplesMv[startIdx] - baseline) * scaleY)
        path.moveTo(0f, firstY.coerceIn(8f, height - 8f))

        for (i in 1 until visibleCount) {
            val sample = samplesMv[startIdx + i]
            val ptX = i * stepX
            val ptY = (centerY - ((sample - baseline) * scaleY)).coerceIn(8f, height - 8f)
            path.lineTo(ptX, ptY)
        }

        drawPath(
            path = path,
            color = Color(0xFF38BDF8),
            style = Stroke(width = 3.5f)
        )
    }
}
