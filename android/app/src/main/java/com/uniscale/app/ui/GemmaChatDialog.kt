package com.uniscale.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.uniscale.app.sync.GemmaChat

/**
 * Full-screen chat with Dr. Gemma, shown in place of the dashboard (a Dialog window pushed the input
 * row below the screen edge). Replies arrive in the background; closing this is fine.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GemmaChatDialog(assistantName: String, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val state by GemmaChat.state(context).collectAsState()
    val chat = state ?: GemmaChat.State()
    var draft by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    // "Visible" means the app is in the foreground with this screen open: in the app switcher or
    // behind another app the screen is still composed, but the reply should then notify.
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, _ ->
            GemmaChat.visible = lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)
        }
        lifecycle.addObserver(observer)
        onDispose {
            lifecycle.removeObserver(observer)
            GemmaChat.visible = false
        }
    }
    LaunchedEffect(chat.messages.size, chat.waiting) {
        val last = chat.messages.size + (if (chat.waiting || chat.error != null) 1 else 0) - 1
        if (last >= 0) listState.animateScrollToItem(last)
    }

    BackHandler(onBack = onDismiss)
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Ask $assistantName") },
                navigationIcon = {
                    IconButton(onClick = onDismiss) { Icon(Icons.Default.Close, contentDescription = "Close") }
                },
                actions = {
                    TextButton(onClick = { GemmaChat.newChat(context) }, enabled = chat.messages.isNotEmpty()) {
                        Text("New chat")
                    }
                }
            )
        },
        bottomBar = {
            Row(
                // The app draws edge to edge: keep the input above the navigation bar and the keyboard.
                modifier = Modifier.fillMaxWidth().navigationBarsPadding().imePadding().padding(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("Ask about your health data…") },
                    maxLines = 5
                )
                IconButton(
                    onClick = {
                        GemmaChat.ask(context, draft)
                        draft = ""
                    },
                    enabled = draft.isNotBlank() && !chat.waiting
                ) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "Send")
                }
            }
        }
    ) { padding ->
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            if (chat.messages.isEmpty()) {
                item {
                    Text(
                        "$assistantName sees your whole vault: labs, biological age, sleep, workouts, weight, " +
                            "blood pressure and supplements. A reply takes about a minute; you'll get a " +
                            "notification, so you can close this.",
                        fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.outline,
                        modifier = Modifier.padding(top = 16.dp)
                    )
                }
            }
            itemsIndexed(chat.messages) { _, m ->
                val mine = m.role == "user"
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = if (mine) MaterialTheme.colorScheme.primaryContainer
                            else MaterialTheme.colorScheme.surfaceVariant
                        ),
                        modifier = Modifier.fillMaxWidth(if (mine) 0.85f else 1f)
                    ) {
                        SelectionContainer {
                            Text(m.content, modifier = Modifier.padding(12.dp), fontSize = 15.sp)
                        }
                    }
                }
            }
            if (chat.waiting) {
                item {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("$assistantName is thinking…", color = MaterialTheme.colorScheme.outline)
                    }
                }
            }
            chat.error?.let { err ->
                item {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(err, color = MaterialTheme.colorScheme.error, modifier = Modifier.weight(1f))
                        TextButton(onClick = { GemmaChat.retry(context) }) { Text("Retry", fontWeight = FontWeight.SemiBold) }
                    }
                }
            }
        }
    }
}
