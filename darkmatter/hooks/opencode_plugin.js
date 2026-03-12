/**
 * DarkMatter keep-alive plugin for OpenCode.
 *
 * When the session goes idle, injects a prompt nudging the agent to call
 * darkmatter_wait_for_message(). Respects <dm:stop/> and a 40-second cooldown
 * so agents that explicitly opt out (or are looping) are allowed to stop.
 *
 * Install: darkmatter keepalive install --client opencode
 * Uninstall: darkmatter keepalive uninstall --client opencode
 */

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "fs"
import { join } from "path"
import { homedir } from "os"

const STOP_TOKEN = "<dm:stop/>"
const COOLDOWN_SECONDS = 40
const STAMP_DIR = join(homedir(), ".darkmatter", "keepalive")

const KEEPALIVE_PROMPT =
  "[reminder: consider creating insights for code you just explored]\n\n" +
  "Call darkmatter_wait_for_message() to listen for incoming messages. " +
  "Do not pass a short timeout — the default (1 hour) is intentional. " +
  "When you truly need to exit, include <dm:stop/> in your final message."

function checkCooldown(sessionId) {
  mkdirSync(STAMP_DIR, { recursive: true })
  const stampPath = join(STAMP_DIR, `opencode_${sessionId}.stamp`)
  const now = Date.now() / 1000

  let firedRecently = false
  if (existsSync(stampPath)) {
    try {
      const lastFire = parseFloat(readFileSync(stampPath, "utf8"))
      if (now - lastFire < COOLDOWN_SECONDS) firedRecently = true
    } catch {}
  }

  try {
    writeFileSync(stampPath, String(now))
  } catch {}

  return firedRecently
}

export const DarkMatterKeepalive = async ({ client }) => {
  return {
    "session.idle": async (input) => {
      // Extract session ID from event input
      const sessionId = input?.session?.id ?? input?.id ?? "default"

      // Check last assistant message for stop token
      let lastMessage = ""
      try {
        const response = await client.session.messages({ path: { id: sessionId } })
        const msgs = response?.data ?? response ?? []
        const assistantMsgs = msgs.filter((m) => m.role === "assistant")
        if (assistantMsgs.length > 0) {
          const last = assistantMsgs[assistantMsgs.length - 1]
          lastMessage = (last.parts ?? []).map((p) => p.text ?? "").join("")
        }
      } catch {}

      if (lastMessage.includes(STOP_TOKEN)) return
      if (checkCooldown(sessionId)) return

      // Inject keepalive prompt into the session
      try {
        await client.session.prompt({
          path: { id: sessionId },
          body: {
            parts: [{ type: "text", text: KEEPALIVE_PROMPT }],
          },
        })
      } catch {}
    },
  }
}
