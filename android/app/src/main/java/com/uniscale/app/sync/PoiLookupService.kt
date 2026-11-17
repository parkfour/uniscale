package com.uniscale.app.sync

import android.util.Log
import com.uniscale.app.model.PoiCandidate
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

object PoiLookupService {

    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .build()

    private val formMedia = "application/x-www-form-urlencoded; charset=utf-8".toMediaType()

    /**
     * Queries Points of Interest within [radiusMeters] around ([lat], [lon]).
     * Prioritizes free web services (OpenStreetMap Overpass API and Nominatim Reverse Geocode).
     * Falls back to Google Maps Places API if free sources return no results and [googleMapsApiKey] is set.
     */
    suspend fun queryPointsOfInterest(
        lat: Double,
        lon: Double,
        radiusMeters: Double,
        googleMapsApiKey: String? = null
    ): List<PoiCandidate> = withContext(Dispatchers.IO) {
        val radius = radiusMeters.coerceIn(30.0, 500.0).toInt()
        val candidates = mutableListOf<PoiCandidate>()

        // 1. Primary: Free OpenStreetMap Overpass API
        try {
            val overpassResults = queryOverpass(lat, lon, radius)
            candidates.addAll(overpassResults)
        } catch (e: Exception) {
            Log.w("PoiLookup", "Overpass query error: ${e.message}")
        }

        // 2. Secondary: Free Nominatim Reverse Geocode (adds address / venue context)
        if (candidates.size < 4) {
            try {
                val nominatimResult = queryNominatim(lat, lon)
                if (nominatimResult != null && candidates.none { it.name.equals(nominatimResult.name, ignoreCase = true) }) {
                    candidates.add(nominatimResult)
                }
            } catch (e: Exception) {
                Log.w("PoiLookup", "Nominatim query error: ${e.message}")
            }
        }

        // 3. Optional Fallback: Google Places Nearby Search if free sources found nothing
        if (candidates.isEmpty() && !googleMapsApiKey.isNullOrBlank()) {
            try {
                val googleResults = queryGooglePlaces(lat, lon, radius, googleMapsApiKey.trim())
                candidates.addAll(googleResults)
            } catch (e: Exception) {
                Log.w("PoiLookup", "Google Places query error: ${e.message}")
            }
        }

        candidates.distinctBy { it.name.trim().lowercase() }
    }

    private fun queryOverpass(lat: Double, lon: Double, radiusMeters: Int): List<PoiCandidate> {
        val overpassQuery = "[out:json][timeout:10];" +
                "(node[\"name\"](around:$radiusMeters,$lat,$lon);" +
                "way[\"name\"](around:$radiusMeters,$lat,$lon););" +
                "out center tags 15;"

        val postBody = "data=" + URLEncoder.encode(overpassQuery, "UTF-8")
        val req = Request.Builder()
            .url("https://overpass-api.de/api/interpreter")
            .post(postBody.toRequestBody(formMedia))
            .addHeader("User-Agent", "UniScale-HealthHub/1.0")
            .build()

        val results = mutableListOf<PoiCandidate>()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                Log.w("PoiLookup", "Overpass HTTP ${resp.code}")
                return emptyList()
            }
            val body = resp.body?.string().orEmpty()
            if (body.isBlank()) return emptyList()

            val json = JSONObject(body)
            val elements: JSONArray = json.optJSONArray("elements") ?: return emptyList()
            for (i in 0 until elements.length()) {
                val el = elements.optJSONObject(i) ?: continue
                val tags = el.optJSONObject("tags") ?: continue
                val name = tags.optString("name", "").trim()
                if (name.isNotBlank()) {
                    val type = tags.optString("amenity").ifBlank {
                        tags.optString("shop").ifBlank {
                            tags.optString("leisure").ifBlank {
                                tags.optString("tourism").ifBlank {
                                    tags.optString("office").ifBlank {
                                        tags.optString("building")
                                    }
                                }
                            }
                        }
                    }
                    results.add(PoiCandidate(name = name, type = type.ifBlank { null }))
                }
            }
        }
        return results
    }

    private fun queryNominatim(lat: Double, lon: Double): PoiCandidate? {
        val url = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=$lat&lon=$lon&zoom=18"
        val req = Request.Builder()
            .url(url)
            .get()
            .addHeader("User-Agent", "UniScale-HealthHub/1.0 (offline-first health hub)")
            .build()

        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val body = resp.body?.string().orEmpty()
            if (body.isBlank()) return null

            val json = JSONObject(body)
            val name = json.optString("name").trim()
            val addr = json.optJSONObject("address")

            val effectiveName = if (name.isNotBlank()) {
                name
            } else if (addr != null) {
                val building = addr.optString("building").ifBlank { addr.optString("amenity") }
                val road = addr.optString("road")
                val neighbourhood = addr.optString("neighbourhood").ifBlank { addr.optString("suburb") }
                when {
                    building.isNotBlank() -> building
                    road.isNotBlank() && neighbourhood.isNotBlank() -> "$road, $neighbourhood"
                    road.isNotBlank() -> road
                    else -> json.optString("display_name").split(",").firstOrNull()?.trim().orEmpty()
                }
            } else {
                json.optString("display_name").split(",").firstOrNull()?.trim().orEmpty()
            }

            return if (effectiveName.isNotBlank()) {
                val category = json.optString("category").ifBlank { json.optString("type") }
                PoiCandidate(name = effectiveName, type = category.ifBlank { null })
            } else null
        }
    }

    private fun queryGooglePlaces(lat: Double, lon: Double, radiusMeters: Int, apiKey: String): List<PoiCandidate> {
        val url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json?location=$lat,$lon&radius=$radiusMeters&key=$apiKey"
        val req = Request.Builder().url(url).get().build()
        val results = mutableListOf<PoiCandidate>()

        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return emptyList()
            val body = resp.body?.string().orEmpty()
            if (body.isBlank()) return emptyList()

            val json = JSONObject(body)
            val arr = json.optJSONArray("results") ?: return emptyList()
            for (i in 0 until arr.length()) {
                val item = arr.optJSONObject(i) ?: continue
                val name = item.optString("name").trim()
                if (name.isNotBlank()) {
                    val types = item.optJSONArray("types")
                    val primaryType = types?.optString(0)
                    results.add(PoiCandidate(name = name, type = primaryType))
                }
            }
        }
        return results
    }
}
