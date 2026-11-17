package com.uniscale.app.sync

import android.content.Context
import android.security.KeyChain
import android.util.Log
import okhttp3.OkHttpClient
import java.net.Socket
import java.security.Principal
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.*

class KeyChainKeyManager(
    private val alias: String,
    private val privateKey: PrivateKey,
    private val certificateChain: Array<X509Certificate>
) : X509ExtendedKeyManager() {

    override fun getClientAliases(keyType: String?, issuers: Array<out Principal>?): Array<String> = arrayOf(alias)

    override fun chooseClientAlias(keyType: Array<out String>?, issuers: Array<out Principal>?, socket: Socket?): String = alias

    override fun chooseEngineClientAlias(keyType: Array<out String>?, issuers: Array<out Principal>?, engine: SSLEngine?): String = alias

    override fun getServerAliases(keyType: String?, issuers: Array<out Principal>?): Array<String>? = null

    override fun chooseServerAlias(keyType: String?, issuers: Array<out Principal>?, socket: Socket?): String? = null

    override fun chooseEngineServerAlias(keyType: String?, issuers: Array<out Principal>?, engine: SSLEngine?): String? = null

    override fun getCertificateChain(alias: String?): Array<X509Certificate>? {
        return if (alias == this.alias) certificateChain else null
    }

    override fun getPrivateKey(alias: String?): PrivateKey? {
        return if (alias == this.alias) privateKey else null
    }

    companion object {
        fun createOkHttpClient(context: Context, certAlias: String?): OkHttpClient {
            val builder = OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(15, TimeUnit.SECONDS)

            if (!certAlias.isNullOrBlank()) {
                try {
                    val privateKey = KeyChain.getPrivateKey(context, certAlias)
                    val certChain = KeyChain.getCertificateChain(context, certAlias)
                    if (privateKey != null && certChain != null && certChain.isNotEmpty()) {
                        val keyManager = KeyChainKeyManager(certAlias, privateKey, certChain)

                        val trustManagerFactory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
                        trustManagerFactory.init(null as java.security.KeyStore?)
                        val trustManagers = trustManagerFactory.trustManagers
                        val x509TrustManager = trustManagers.first { it is X509TrustManager } as X509TrustManager

                        val sslContext = SSLContext.getInstance("TLS")
                        sslContext.init(arrayOf(keyManager), trustManagers, SecureRandom())

                        builder.sslSocketFactory(sslContext.socketFactory, x509TrustManager)
                        Log.i("KeyChainKeyManager", "Configured client-certificate OkHttpClient")
                    } else {
                        Log.w("KeyChainKeyManager", "Certificate or private key not found for the selected alias")
                    }
                } catch (e: Exception) {
                    Log.e("KeyChainKeyManager", "Failed initializing KeyChain mTLS: ${e.message}", e)
                }
            }

            return builder.build()
        }
    }
}
