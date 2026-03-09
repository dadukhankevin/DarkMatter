"""Stdout adapters for converting agent subprocess output into prose chunks.

Each adapter understands a specific CLI tool's output format and extracts
only the human-readable prose, discarding tool chrome, spinners, and UI.

To add support for a new agent CLI:
1. Subclass StdoutAdapter
2. Implement feed(data: bytes) -> list[str]
3. Register it in get_adapter()
"""

import re


class StdoutAdapter:
    """Base adapter — strips ANSI and sends sections with enough words."""

    MIN_WORDS = 12

    _ansi_re = re.compile(
        r'\x1b\[[0-9;?]*[A-Za-z]'
        r'|\x1b\][^\x07]*\x07'
        r'|\x1b[()][A-Z0-9]'
        r'|\x1b[78=>cDEHMNOZ]'
    )

    def __init__(self):
        self._esc_buf = ""

    def feed(self, data: bytes) -> list[str]:
        """Feed raw bytes, return list of prose sections."""
        raw = self._esc_buf + data.decode("utf-8", errors="replace")
        self._esc_buf = ""

        # Buffer incomplete escape at end
        last_esc = raw.rfind('\x1b')
        if last_esc >= 0 and not self._ansi_re.search(raw[last_esc:]):
            self._esc_buf = raw[last_esc:]
            raw = raw[:last_esc]

        text = self._ansi_re.sub('', raw).replace('\r', '')
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        if not text:
            return []

        sections = [s.strip() for s in text.split('\n\n') if s.strip()]
        return [s for s in sections if len(s.split()) >= self.MIN_WORDS]

    def flush(self) -> list[str]:
        """Flush any remaining buffered content."""
        return []


class ClaudeCodeAdapter(StdoutAdapter):
    """Extract prose from Claude Code's TUI output.

    Claude Code marks each output block with a bullet marker:
    - Default color (white) ⏺ = prose text for the human
    - Colored (green) ⏺ = tool call, skip
    - ⎿ = tool result / indented content, skip

    Only text following white ⏺ markers is emitted. Cursor-forward
    sequences are converted to spaces to preserve word boundaries.
    """

    # States for the block parser
    _IDLE = 0       # Outside any block, waiting for ⏺
    _PENDING = 1    # Saw ⏺, waiting for first text to determine prose vs tool
    _PROSE = 2      # Inside a prose block (text after ⏺ in default color)
    _SKIP = 3       # Inside a tool call block (text after ⏺ in explicit color)

    def __init__(self):
        super().__init__()
        self._colored = False  # True when explicit foreground color is set
        self._state = self._IDLE
        self._prose_buf = []

    def feed(self, data: bytes) -> list[str]:
        raw = self._esc_buf + data.decode("utf-8", errors="replace")
        self._esc_buf = ""

        results = []
        i = 0
        n = len(raw)

        while i < n:
            ch = raw[i]

            # --- ANSI escape sequences ---
            if ch == '\x1b':
                if i + 1 >= n:
                    self._esc_buf = raw[i:]
                    break

                nch = raw[i + 1]

                if nch == '[':
                    # CSI sequence: \x1b[ <params> <letter>
                    j = i + 2
                    while j < n and (raw[j].isdigit() or raw[j] in ';?'):
                        j += 1
                    if j >= n:
                        self._esc_buf = raw[i:]
                        break
                    params = raw[i + 2:j]
                    cmd = raw[j]
                    if cmd == 'm':
                        # SGR — track foreground color state
                        parts = params.split(';') if params else ['0']
                        k = 0
                        while k < len(parts):
                            try:
                                code = int(parts[k])
                            except ValueError:
                                k += 1
                                continue
                            if code == 0 or code == 39:
                                self._colored = False
                            elif code == 38:
                                # 38;2;r;g;b = 24-bit color or 38;5;n = 256-color
                                # Check if it's white (255,255,255) → treat as default
                                if k + 1 < len(parts):
                                    try:
                                        mode = int(parts[k + 1])
                                    except ValueError:
                                        mode = 0
                                    if mode == 2 and k + 4 < len(parts):
                                        # 24-bit: 38;2;r;g;b
                                        try:
                                            r, g, b = int(parts[k+2]), int(parts[k+3]), int(parts[k+4])
                                        except (ValueError, IndexError):
                                            r, g, b = 0, 0, 0
                                        # White or near-white → default color
                                        if r >= 240 and g >= 240 and b >= 240:
                                            self._colored = False
                                        else:
                                            self._colored = True
                                        k += 5
                                        continue
                                    elif mode == 5 and k + 2 < len(parts):
                                        # 256-color: 38;5;n
                                        self._colored = True
                                        k += 3
                                        continue
                                self._colored = True
                            elif (30 <= code <= 37) or (90 <= code <= 97):
                                self._colored = True
                            k += 1
                    elif cmd == 'C' and self._state == self._PROSE:
                        # Cursor forward → spaces (preserves word boundaries)
                        count = int(params) if params else 1
                        self._prose_buf.append(' ' * count)
                    i = j + 1

                elif nch == ']':
                    # OSC sequence — skip to BEL
                    j = i + 2
                    while j < n and raw[j] != '\x07':
                        j += 1
                    if j >= n:
                        self._esc_buf = raw[i:]
                        break
                    i = j + 1

                elif nch in '()':
                    if i + 2 < n:
                        i += 3
                    else:
                        self._esc_buf = raw[i:]
                        break

                elif nch in '78=>cDEHMNOZ':
                    i += 2

                else:
                    i += 1
                continue

            # --- Block markers ---
            if ch == '⏺':
                # Flush any accumulated prose
                self._flush_prose(results)
                # Don't decide yet — wait for the text color after the marker
                self._state = self._PENDING
                i += 1
                continue

            if ch == '⎿':
                # Tool result — stop capturing
                self._flush_prose(results)
                self._state = self._SKIP
                i += 1
                continue

            # --- Content ---
            # Bare \r (not \r\n) = terminal line overwrite (UI redraw).
            # End prose capture — the response text is done.
            if ch == '\r':
                if i + 1 < n and raw[i + 1] == '\n':
                    # \r\n = normal newline in prose
                    if self._state == self._PROSE:
                        self._prose_buf.append('\n')
                    i += 2
                    continue
                else:
                    # Bare \r = cursor return to start of line (UI redraw)
                    if self._state == self._PROSE:
                        self._flush_prose(results)
                        self._state = self._IDLE
                    i += 1
                    continue
            # Skip other control chars and whitespace outside prose
            if ch in '\n\t' or (ch == ' ' and self._state != self._PROSE):
                i += 1
                continue

            # Resolve pending state on first real text character
            if self._state == self._PENDING:
                if self._colored:
                    self._state = self._SKIP
                else:
                    self._state = self._PROSE

            if self._state == self._PROSE:
                self._prose_buf.append(ch)

            i += 1

        return results

    def flush(self) -> list[str]:
        """Flush any remaining prose buffer (called when stream ends)."""
        results = []
        self._flush_prose(results)
        return results

    def _flush_prose(self, results: list[str]) -> None:
        if self._prose_buf:
            text = ''.join(self._prose_buf).strip()
            self._prose_buf = []
            if text:
                results.append(text)


def get_adapter(client_config: dict) -> StdoutAdapter:
    """Select the right adapter based on the active client config."""
    command = client_config.get("command", "")
    if "claude" in command.lower():
        return ClaudeCodeAdapter()
    return StdoutAdapter()
