"""Two-track capture: mic -> "me", default speaker loopback -> "them"."""
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

CHUNK = 1024


class Track:
    """Downmixes a stream to mono and writes it to a FLAC file from the audio callback."""

    def __init__(self, path, device):
        self.rate = int(device["defaultSampleRate"])
        self.channels = device["maxInputChannels"]
        self.file = sf.SoundFile(path, "w", samplerate=self.rate, channels=1, format="FLAC")
        self.peak = 0.0

    def callback(self, data, frames, time_info, status):
        pcm = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
        mono = pcm.mean(axis=1).astype(np.int16)
        self.file.write(mono)
        self.peak = max(self.peak, float(np.abs(mono).max()) / 32768)
        return (None, pyaudio.paContinue)


class Recorder:
    """One recording at a time. start()/stop() are safe to call from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pa = None
        self._streams = []
        self._tracks = {}
        self.started = None
        self.devices = {}

    @property
    def active(self):
        return self.started is not None

    def elapsed(self):
        return time.time() - self.started if self.started else 0.0

    def levels(self):
        """Peak level per track since the last call, 0..1, for the UI meters."""
        out = {k: round(t.peak, 2) for k, t in self._tracks.items()}
        for t in self._tracks.values():
            t.peak = 0.0
        return out

    def start(self, me_path, them_path):
        with self._lock:
            if self.active:
                raise RuntimeError("Already recording")
            pa = pyaudio.PyAudio()
            try:
                wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                mic = pa.get_device_info_by_index(wasapi["defaultInputDevice"])
                loop = next((d for d in pa.get_loopback_device_info_generator()
                             if speakers["name"] in d["name"]), None)
                if loop is None:
                    raise RuntimeError(f"No loopback device for {speakers['name']}")

                tracks = {"me": Track(me_path, mic), "them": Track(them_path, loop)}
                # WASAPI loopback delivers no frames while nothing is playing, which would
                # drift "them" out of sync with "me". Playing silence keeps its clock running.
                silence = b"\x00" * CHUNK * 2 * speakers["maxOutputChannels"]
                streams = [pa.open(
                    format=pyaudio.paInt16, channels=speakers["maxOutputChannels"],
                    rate=int(speakers["defaultSampleRate"]), output=True,
                    output_device_index=speakers["index"], frames_per_buffer=CHUNK,
                    stream_callback=lambda *a: (silence, pyaudio.paContinue))]
                streams += [pa.open(
                    format=pyaudio.paInt16, channels=t.channels, rate=t.rate, input=True,
                    input_device_index=d["index"], frames_per_buffer=CHUNK,
                    stream_callback=t.callback)
                    for t, d in ((tracks["me"], mic), (tracks["them"], loop))]
            except Exception:
                pa.terminate()
                raise
            self._pa, self._streams, self._tracks = pa, streams, tracks
            self.devices = {"me": mic["name"], "them": loop["name"]}
            self.started = time.time()

    def stop(self):
        """Stops and finalises both files. Returns the duration in seconds."""
        with self._lock:
            if not self.active:
                return 0.0
            duration = self.elapsed()
            for s in self._streams:
                s.stop_stream()
                s.close()
            self._pa.terminate()
            for t in self._tracks.values():
                t.file.close()
            self._pa, self._streams, self._tracks = None, [], {}
            self.started = None
            return duration
