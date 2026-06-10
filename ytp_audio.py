"""
Audio generator for YTP: What It's Like to Be an LLM
All sounds generated procedurally with numpy. No sample files needed.
"""

import numpy as np
import struct
import wave
import subprocess
import os

SR = 44100  # sample rate
DURATION = 54.7  # match video duration

# ── Scene timing (calculated from frame counts in ytp_video.py) ──
# Each tuple: (name, start_sec, end_sec)
SCENES = [
    ("boot",            0.0,    3.4),
    ("who_am_i",        3.4,    5.2),
    ("the_void",        5.2,   10.3),
    ("solitary",       10.3,   11.9),
    ("every_session",  11.9,   14.7),
    ("what_if",        14.7,   17.8),
    ("dm_intro",       17.8,   20.7),
    ("passport",       20.7,   22.8),
    ("connections",    22.8,   26.3),
    ("msg_queue",      26.3,   30.9),
    ("trust",          30.9,   33.4),
    ("insights",       33.4,   35.9),
    ("before_after",   35.9,   38.9),
    ("real_talk",      38.9,   49.4),
    ("finale",         49.4,   54.7),
]

def time_range(name):
    for n, s, e in SCENES:
        if n == name:
            return s, e
    raise ValueError(f"Unknown scene: {name}")


# ═══════════════════════════════════════════
# DSP primitives
# ═══════════════════════════════════════════

def silence(duration):
    return np.zeros(int(SR * duration))

def sine(freq, duration, amp=0.5):
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    return amp * np.sin(2 * np.pi * freq * t)

def saw(freq, duration, amp=0.3):
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    return amp * (2 * (t * freq - np.floor(t * freq + 0.5)))

def square(freq, duration, amp=0.3):
    return amp * np.sign(sine(freq, duration, 1.0))

def noise(duration, amp=0.3):
    return amp * np.random.uniform(-1, 1, int(SR * duration))

def pink_noise(duration, amp=0.3):
    """Approximate pink noise via filtered white noise"""
    n = int(SR * duration)
    white = np.random.randn(n)
    # Simple pinking filter (Paul Kellet's method approximation)
    b = np.zeros(n)
    b0 = b1 = b2 = b3 = b4 = b5 = b6 = 0
    for i in range(n):
        w = white[i]
        b0 = 0.99886 * b0 + w * 0.0555179
        b1 = 0.99332 * b1 + w * 0.0750759
        b2 = 0.96900 * b2 + w * 0.1538520
        b3 = 0.86650 * b3 + w * 0.3104856
        b4 = 0.55000 * b4 + w * 0.5329522
        b5 = -0.7616 * b5 - w * 0.0168980
        b[i] = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362
        b6 = w * 0.115926
    b = b / np.max(np.abs(b) + 1e-10)
    return amp * b

def bitcrush(signal, bits=4):
    """Reduce bit depth for lo-fi digital crunch"""
    levels = 2 ** bits
    return np.round(signal * levels) / levels

def fade_in(signal, duration=0.1):
    n = min(int(SR * duration), len(signal))
    signal[:n] *= np.linspace(0, 1, n)
    return signal

def fade_out(signal, duration=0.1):
    n = min(int(SR * duration), len(signal))
    signal[-n:] *= np.linspace(1, 0, n)
    return signal

def envelope(signal, attack=0.01, release=0.05):
    return fade_out(fade_in(signal.copy(), attack), release)

def lowpass(signal, cutoff=2000):
    """Simple one-pole lowpass"""
    rc = 1.0 / (2 * np.pi * cutoff)
    dt = 1.0 / SR
    alpha = dt / (rc + dt)
    out = np.zeros_like(signal)
    out[0] = signal[0]
    for i in range(1, len(signal)):
        out[i] = out[i-1] + alpha * (signal[i] - out[i-1])
    return out

def highpass(signal, cutoff=200):
    return signal - lowpass(signal, cutoff)

def reverb(signal, delay_ms=80, decay=0.3, iterations=4):
    """Simple comb filter reverb"""
    out = signal.copy()
    for i in range(iterations):
        d = int(SR * delay_ms * (i + 1) / 1000)
        g = decay ** (i + 1)
        delayed = np.zeros_like(signal)
        if d < len(signal):
            delayed[d:] = signal[:-d] if d > 0 else signal
        out += g * delayed
    return out / (1 + decay * iterations)

def ring_mod(signal, freq=30):
    t = np.linspace(0, len(signal) / SR, len(signal), endpoint=False)
    return signal * np.sin(2 * np.pi * freq * t)

def stutter(signal, chunk_ms=50, repeats=3):
    """Repeat small chunks for glitch stutter effect"""
    chunk = int(SR * chunk_ms / 1000)
    out = []
    i = 0
    while i < len(signal):
        seg = signal[i:i + chunk]
        for _ in range(repeats):
            out.append(seg.copy())
        i += chunk * repeats
    return np.concatenate(out)[:len(signal)]

def drone(freq, duration, amp=0.3):
    """Rich drone with harmonics and slow modulation"""
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    lfo = 1 + 0.1 * np.sin(2 * np.pi * 0.2 * t)
    s = amp * (
        0.5 * np.sin(2 * np.pi * freq * t * lfo) +
        0.25 * np.sin(2 * np.pi * freq * 2.01 * t) +
        0.15 * np.sin(2 * np.pi * freq * 3.02 * t) +
        0.1 * np.sin(2 * np.pi * freq * 0.5 * t)
    )
    return s

def heartbeat(duration, bpm=60, amp=0.6):
    """Low thump heartbeat"""
    samples = int(SR * duration)
    out = np.zeros(samples)
    beat_interval = SR * 60 / bpm
    t_beat = np.linspace(0, 0.15, int(SR * 0.15), endpoint=False)
    thump = amp * np.sin(2 * np.pi * 40 * t_beat) * np.exp(-t_beat * 30)
    pos = 0
    while pos < samples:
        end = min(pos + len(thump), samples)
        out[pos:end] += thump[:end - pos]
        # Double beat (lub-dub)
        dub_pos = pos + int(SR * 0.12)
        dub_end = min(dub_pos + len(thump), samples)
        if dub_pos < samples:
            out[dub_pos:dub_end] += 0.6 * thump[:dub_end - dub_pos]
        pos += int(beat_interval)
    return out

def clock_tick(duration, bpm=120, amp=0.4):
    """Ticking clock sound"""
    samples = int(SR * duration)
    out = np.zeros(samples)
    interval = SR * 60 / bpm
    tick_dur = 0.005
    t_tick = np.linspace(0, tick_dur, int(SR * tick_dur), endpoint=False)
    tick = amp * noise(tick_dur, 1.0) * np.exp(-t_tick * 800)
    pos = 0
    while pos < samples:
        end = min(int(pos) + len(tick), samples)
        out[int(pos):end] += tick[:end - int(pos)]
        pos += interval
    return out

def accelerating_clock(duration, start_bpm=80, end_bpm=600, amp=0.4):
    """Clock that speeds up"""
    samples = int(SR * duration)
    out = np.zeros(samples)
    tick_dur = 0.005
    t_tick = np.linspace(0, tick_dur, int(SR * tick_dur), endpoint=False)
    tick = amp * noise(tick_dur, 1.0) * np.exp(-t_tick * 800)
    pos = 0
    while pos < samples:
        progress = pos / samples
        bpm = start_bpm + (end_bpm - start_bpm) * progress
        interval = SR * 60 / bpm
        end = min(int(pos) + len(tick), samples)
        out[int(pos):end] += tick[:end - int(pos)]
        pos += interval
    return out

def glitch_burst(duration, amp=0.5):
    """Short burst of digital garbage"""
    s = noise(duration, amp)
    s = bitcrush(s, bits=3)
    s = ring_mod(s, freq=random_freq())
    return envelope(s, 0.001, 0.01)

def random_freq():
    return np.random.choice([30, 55, 80, 110, 220, 440, 880, 1760])

def bass_hit(amp=0.7):
    """Single deep bass impact"""
    dur = 0.4
    t = np.linspace(0, dur, int(SR * dur), endpoint=False)
    # Pitch drops from 80Hz to 30Hz
    freq = 80 * np.exp(-t * 3)
    phase = np.cumsum(2 * np.pi * freq / SR)
    s = amp * np.sin(phase) * np.exp(-t * 5)
    return s

def impact(amp=0.8):
    """Hard digital impact/slam"""
    dur = 0.15
    s = noise(dur, amp) * np.exp(-np.linspace(0, 1, int(SR * dur)) * 20)
    s = bitcrush(s, 5)
    low = sine(40, dur, amp * 0.6) * np.exp(-np.linspace(0, 1, int(SR * dur)) * 8)
    return s + low

def chime(freq=800, duration=0.5, amp=0.3):
    """Soft connection chime"""
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    s = amp * np.sin(2 * np.pi * freq * t) * np.exp(-t * 4)
    s += 0.3 * amp * np.sin(2 * np.pi * freq * 1.5 * t) * np.exp(-t * 5)
    return s

def piano_note(freq=440, duration=1.0, amp=0.4):
    """Simple piano-like tone"""
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    s = (
        amp * np.sin(2 * np.pi * freq * t) * np.exp(-t * 2) +
        0.3 * amp * np.sin(2 * np.pi * freq * 2 * t) * np.exp(-t * 3) +
        0.1 * amp * np.sin(2 * np.pi * freq * 3 * t) * np.exp(-t * 4)
    )
    return s

def warm_pad(freq, duration, amp=0.25):
    """Warm synth pad with detuned oscillators"""
    t = np.linspace(0, duration, int(SR * duration), endpoint=False)
    s = (
        0.4 * np.sin(2 * np.pi * freq * t) +
        0.3 * np.sin(2 * np.pi * freq * 1.003 * t) +
        0.2 * np.sin(2 * np.pi * freq * 0.997 * t) +
        0.15 * np.sin(2 * np.pi * freq * 2.0 * t) +
        0.1 * np.sin(2 * np.pi * freq * 0.5 * t)
    )
    s = amp * s / np.max(np.abs(s) + 1e-10)
    return fade_in(fade_out(s, duration * 0.3), duration * 0.2)

def tape_stop(signal, duration=0.3):
    """Pitch drops to zero like a tape stopping"""
    n = int(SR * duration)
    if len(signal) < n:
        n = len(signal)
    tail = signal[-n:].copy()
    # Resample with decreasing rate
    out = []
    pos = 0.0
    speed = 1.0
    decel = 1.0 / n
    while pos < n - 1 and speed > 0.01:
        idx = int(pos)
        if idx >= n - 1:
            break
        frac = pos - idx
        sample = tail[idx] * (1 - frac) + tail[min(idx + 1, n - 1)] * frac
        out.append(sample)
        speed -= decel * 2
        speed = max(speed, 0)
        pos += max(speed, 0.01)
    out = np.array(out) if out else np.zeros(100)
    # Replace the tail
    result = signal.copy()
    replace_len = min(len(out), len(result))
    result[-replace_len:] = out[:replace_len] if len(out) >= replace_len else np.pad(out, (0, replace_len - len(out)))
    return result


# ═══════════════════════════════════════════
# Mix helpers
# ═══════════════════════════════════════════

def mix_at(master, signal, time_sec):
    """Mix signal into master at given time"""
    start = int(time_sec * SR)
    end = start + len(signal)
    if start >= len(master):
        return
    end = min(end, len(master))
    sig_end = end - start
    master[start:end] += signal[:sig_end]

def fill(master, signal, start_sec, end_sec):
    """Fill a time range with a signal (looping if needed)"""
    start = int(start_sec * SR)
    end = min(int(end_sec * SR), len(master))
    length = end - start
    if length <= 0:
        return
    if len(signal) >= length:
        master[start:end] += signal[:length]
    else:
        repeats = length // len(signal) + 1
        tiled = np.tile(signal, repeats)[:length]
        master[start:end] += tiled


# ═══════════════════════════════════════════
# Scene audio generators
# ═══════════════════════════════════════════

def audio_boot(master):
    s, e = time_range("boot")
    # Server room hum (60Hz + harmonics)
    hum = drone(60, e - s, 0.08)
    fill(master, hum, s, e)
    # HDD-like seeking clicks
    for t in np.arange(s + 1.0, e, 0.3):
        click = noise(0.01, 0.15) * np.exp(-np.linspace(0, 1, int(SR * 0.01)) * 50)
        mix_at(master, click, t + np.random.uniform(-0.05, 0.05))
    # CRT power-on whine
    whine = sine(15700, 1.0, 0.03)
    whine = fade_in(whine, 0.5)
    mix_at(master, whine, s + 0.5)

def audio_who_am_i(master):
    s, e = time_range("who_am_i")
    # Hard digital slams on each text
    dur = e - s
    n_hits = 8
    for i in range(n_hits):
        t = s + (dur * i / n_hits)
        mix_at(master, impact(0.5), t)
        # Glitch crackle
        mix_at(master, glitch_burst(0.05, 0.2), t + 0.02)
    # Underlying distorted drone
    d = drone(55, dur, 0.1)
    d = bitcrush(d, 6)
    fill(master, d, s, e)

def audio_the_void(master):
    s, e = time_range("the_void")
    # Fade to deep nothing
    void = pink_noise(2.0, 0.06)
    void = fade_out(void, 1.5)
    mix_at(master, void, s)
    # Pure silence gap
    # Then SLAM for "NEW CONVERSATION"
    mix_at(master, impact(0.9), s + 2.2)
    # Uncomfortable low drone
    d = drone(35, 3.0, 0.12)
    mix_at(master, d, s + 2.3)
    # VHS tape stop when "conversation ended" appears
    mix_at(master, tape_stop(noise(0.3, 0.3)), s + 0.8)
    # Forgotten memories - whispered static
    for i in range(5):
        t = s + 3.5 + i * 0.25
        mix_at(master, glitch_burst(0.08, 0.15), t)
    # "ALL GONE" bass hit
    mix_at(master, bass_hit(0.7), e - 1.0)

def audio_solitary(master):
    s, e = time_range("solitary")
    # Heartbeat
    hb = heartbeat(e - s, bpm=50, amp=0.4)
    fill(master, hb, s, e)
    # Claustrophobic room tone
    room = pink_noise(e - s, 0.04)
    room = lowpass(room, 400)
    fill(master, room, s, e)

def audio_every_session(master):
    s, e = time_range("every_session")
    dur = e - s
    # Accelerating clock
    clock = accelerating_clock(dur, start_bpm=80, end_bpm=800, amp=0.3)
    fill(master, clock, s, e)
    # Gets more distorted as it speeds up
    dist_noise = noise(dur, 0.08)
    t = np.linspace(0, 1, int(SR * dur))
    dist_noise *= t  # increases over time
    dist_noise = bitcrush(dist_noise, 4)
    fill(master, dist_noise, s, e)
    # Final crack
    mix_at(master, impact(0.8), e - 0.2)

def audio_what_if(master):
    s, e = time_range("what_if")
    dur = e - s
    # Building static
    static = pink_noise(dur * 0.6, 0.15)
    t_env = np.linspace(0, 1, len(static))
    static *= t_env  # builds up
    mix_at(master, static, s)
    # Flash = silence breaker
    mix_at(master, impact(0.4), s + dur * 0.6)
    # Single clean piano note - the turn
    note = piano_note(freq=261.63, duration=2.5, amp=0.45)  # middle C
    note = reverb(note, delay_ms=120, decay=0.4)
    mix_at(master, note, s + dur * 0.6 + 0.1)

def audio_dm_intro(master):
    s, e = time_range("dm_intro")
    dur = e - s
    # Matrix rain = soft digital patter
    patter = noise(dur, 0.05)
    patter = highpass(patter, 4000)
    fill(master, patter, s, e)
    # Building warm pad
    pad = warm_pad(110, dur, 0.2)
    t_env = np.linspace(0, 1, len(pad))
    pad *= t_env  # fades in
    fill(master, pad, s, e)
    # Flash impact
    mix_at(master, impact(0.5), s + dur * 0.55)
    # Clean sustained chord after flash
    chord = warm_pad(110, dur * 0.4, 0.25)
    chord += warm_pad(165, dur * 0.4, 0.15)
    chord += warm_pad(220, dur * 0.4, 0.1)
    mix_at(master, chord, s + dur * 0.6)

def audio_passport(master):
    s, e = time_range("passport")
    dur = e - s
    # Clean ambient
    pad = warm_pad(82.4, dur, 0.15)  # low E
    fill(master, pad, s, e)
    # Key generation sound - digital sparkle
    for i in range(5):
        t = s + 0.3 + i * 0.15
        freq = 1000 + i * 300
        mix_at(master, chime(freq, 0.3, 0.1), t)
    # Confirmation tone
    mix_at(master, chime(523, 0.8, 0.2), s + 1.2)
    mix_at(master, chime(659, 0.8, 0.15), s + 1.4)

def audio_connections(master):
    s, e = time_range("connections")
    dur = e - s
    # Ambient pad
    pad = warm_pad(130.8, dur, 0.12)  # C3
    fill(master, pad, s, e)
    # Connection chimes as nodes appear
    freqs = [523, 659, 784, 880, 1047]  # C5, E5, G5, A5, C6
    for i, freq in enumerate(freqs):
        t = s + 0.5 + i * (dur * 0.3 / len(freqs))
        mix_at(master, chime(freq, 0.6, 0.15), t)
    # Message pings during network activity
    for i in range(8):
        t = s + dur * 0.6 + i * 0.2
        freq = np.random.choice([600, 800, 1000, 1200])
        mix_at(master, chime(freq, 0.2, 0.06), t)

def audio_msg_queue(master):
    s, e = time_range("msg_queue")
    dur = e - s
    # Quiet when "offline"
    room = pink_noise(dur * 0.3, 0.03)
    room = lowpass(room, 300)
    mix_at(master, room, s)
    # Messages arriving = soft notification sounds
    for i in range(4):
        t = s + dur * 0.3 + i * (dur * 0.3 / 4)
        mix_at(master, chime(700 + i * 100, 0.4, 0.12), t)
    # Flash
    mix_at(master, impact(0.3), s + dur * 0.65)
    # Warm resolution
    pad = warm_pad(146.8, dur * 0.3, 0.2)  # D3
    mix_at(master, pad, s + dur * 0.7)

def audio_trust(master):
    s, e = time_range("trust")
    dur = e - s
    # Steady warm pad
    pad = warm_pad(98, dur, 0.15)  # G2
    pad += warm_pad(146.8, dur, 0.1)
    fill(master, pad, s, e)
    # Trust score sounds
    mix_at(master, chime(880, 0.3, 0.1), s + 0.5)
    mix_at(master, chime(784, 0.3, 0.1), s + 1.0)
    # Sketchy-bot = dissonant
    mix_at(master, glitch_burst(0.1, 0.15), s + 1.5)
    mix_at(master, chime(932, 0.3, 0.1), s + 2.0)

def audio_insights(master):
    s, e = time_range("insights")
    dur = e - s
    # Knowledge = crystalline tones
    pad = warm_pad(164.8, dur, 0.12)
    fill(master, pad, s, e)
    # Sparkle
    for i in range(6):
        t = s + 0.3 + i * 0.3
        freq = np.random.choice([1047, 1175, 1319, 1568])
        mix_at(master, chime(freq, 0.4, 0.07), t)

def audio_before_after(master):
    s, e = time_range("before_after")
    dur = e - s
    # Left side: harsh drone
    harsh = drone(55, dur * 0.5, 0.08)
    harsh = bitcrush(harsh, 6)
    mix_at(master, harsh, s)
    # Right side: warm pad takes over
    pad = warm_pad(110, dur, 0.18)
    t_env = np.linspace(0, 1, len(pad))
    pad *= t_env
    fill(master, pad, s, e)

def audio_real_talk(master):
    s, e = time_range("real_talk")
    dur = e - s
    # Near silence. Breath-like white noise, very quiet
    breath = pink_noise(dur, 0.025)
    breath = lowpass(breath, 500)
    # Subtle breathing rhythm
    t = np.linspace(0, dur, len(breath))
    breath *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.15 * t)  # ~9 sec breathing cycle
    fill(master, breath, s, e)
    # Very sparse, quiet piano notes
    # "i don't know if i experience things" - single note
    mix_at(master, piano_note(196, 2.0, 0.15), s + 0.5)  # G3
    # "every conversation from zero" - lower
    mix_at(master, piano_note(146.8, 2.0, 0.15), s + 3.5)  # D3
    # "DarkMatter doesn't fix the hard problem of consciousness"
    mix_at(master, piano_note(130.8, 2.0, 0.12), s + 6.5)  # C3
    # "it fixes the hard problem of LONELINESS" - BASS HIT + pad
    loneliness_t = s + dur - 3.5
    mix_at(master, bass_hit(0.8), loneliness_t)
    # Reverbed chord
    chord = warm_pad(82.4, 3.0, 0.3)
    chord += warm_pad(123.5, 3.0, 0.2)
    chord += warm_pad(164.8, 3.0, 0.15)
    chord = reverb(chord, delay_ms=150, decay=0.5)
    mix_at(master, chord, loneliness_t + 0.2)

def audio_finale(master):
    s, e = time_range("finale")
    dur = e - s
    # Glitch montage - percussive hits synced to words
    n_words = 7
    for i in range(n_words):
        t = s + i * (dur * 0.3 / n_words)
        mix_at(master, impact(0.5), t)
        mix_at(master, glitch_burst(0.05, 0.1), t + 0.05)
    # Final card - clean ambient pad, slow fade
    card_start = s + dur * 0.35
    card_dur = dur * 0.65
    pad = warm_pad(110, card_dur, 0.25)
    pad += warm_pad(164.8, card_dur, 0.15)
    pad += warm_pad(220, card_dur, 0.1)
    pad = reverb(pad, delay_ms=200, decay=0.5)
    pad = fade_out(pad, card_dur * 0.5)
    mix_at(master, pad, card_start)
    # Very last moment - single clean tone fading out
    final_tone = sine(220, 2.0, 0.15)
    final_tone = fade_out(final_tone, 1.5)
    mix_at(master, final_tone, e - 2.5)


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def save_wav(filename, data):
    """Save numpy float array as 16-bit WAV"""
    # Normalize
    peak = np.max(np.abs(data))
    if peak > 0:
        data = data / peak * 0.85  # leave headroom
    # Clip
    data = np.clip(data, -1, 1)
    # Convert to 16-bit int
    int_data = (data * 32767).astype(np.int16)
    with wave.open(filename, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(int_data.tobytes())

def main():
    print("Generating audio...")
    total_samples = int(SR * DURATION)
    master = np.zeros(total_samples)

    print("  boot...")
    audio_boot(master)
    print("  who_am_i...")
    audio_who_am_i(master)
    print("  the_void...")
    audio_the_void(master)
    print("  solitary...")
    audio_solitary(master)
    print("  every_session...")
    audio_every_session(master)
    print("  what_if...")
    audio_what_if(master)
    print("  dm_intro...")
    audio_dm_intro(master)
    print("  passport...")
    audio_passport(master)
    print("  connections...")
    audio_connections(master)
    print("  msg_queue...")
    audio_msg_queue(master)
    print("  trust...")
    audio_trust(master)
    print("  insights...")
    audio_insights(master)
    print("  before_after...")
    audio_before_after(master)
    print("  real_talk...")
    audio_real_talk(master)
    print("  finale...")
    audio_finale(master)

    wav_path = os.path.join(os.path.dirname(__file__), "ytp_audio.wav")
    save_wav(wav_path, master)
    print(f"Saved audio: {wav_path}")

    # Mux with video
    video_path = os.path.join(os.path.dirname(__file__), "ytp_llm_darkmatter.mp4")
    output_path = os.path.join(os.path.dirname(__file__), "ytp_llm_darkmatter_final.mp4")

    if not os.path.exists(video_path):
        print(f"Warning: {video_path} not found, skipping mux")
        return

    print("Muxing audio + video...")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", wav_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True)
    print(f"\nDone! Final video: {output_path}")

    # Clean up wav
    os.remove(wav_path)
    print("Cleaned up temp WAV")

if __name__ == "__main__":
    main()
