# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Context Recall daemon.

Produces a directory-mode bundle at dist/context-recall-daemon/ containing
the daemon binary and all dependencies. Directory mode is preferred over
one-file because:
  - Individual binaries can be codesigned on macOS
  - Faster startup (no temp extraction)
  - Easier debugging

Build: pyinstaller context-recall.spec
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None

# Collect native libraries and data files for key dependencies.
datas = []
binaries = []

# MLX and MLX Whisper: Apple Silicon ML framework.
try:
    binaries += collect_dynamic_libs("mlx")
    binaries += collect_dynamic_libs("mlx_metal")
    datas += collect_data_files("mlx")
    datas += collect_data_files("mlx_whisper")
except Exception:
    pass  # Not available on non-Apple Silicon

# sounddevice: PortAudio shared library.
binaries += collect_dynamic_libs("sounddevice")
datas += collect_data_files("sounddevice")

# soundfile: libsndfile shared library.
binaries += collect_dynamic_libs("soundfile")
datas += collect_data_files("soundfile")

# speechbrain ships yaml/log-config data its inference classes read.
datas += collect_data_files("speechbrain")

a = Analysis(
    ["src/__main__.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # Core pipeline
        "huggingface_hub",
        "tokenizers",
        "numpy",
        "sounddevice",
        "soundfile",
        # MLX Whisper (Apple Silicon GPU transcription)
        "mlx",
        "mlx_whisper",
        # API server
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "starlette",
        "websockets",
        "aiosqlite",
        "pydantic",
        # Summarisation backends
        "anthropic",
        "httpx",
        # Output writers
        "notion_client",
        "slugify",
        "yaml",
        # Voice identification + semantic search (torch stack)
        "torch",
        "torchaudio",
        "speechbrain",
        "hyperpyyaml",
        "sentence_transformers",
        # The full src.* tree (pipeline, API routes, intelligence modules,
        # platform adapters, utilities) is enumerated by
        # collect_submodules("src") below — this picks up lazy imports like
        # src.action_items.* and src.api.routes.* without needing to list
        # every module by hand.
        "src",
        "src.main",
    ]
    + collect_submodules("src")
    + collect_submodules("uvicorn")
    + collect_submodules("starlette")
    + collect_submodules("mlx")
    + collect_submodules("mlx_whisper")
    # speechbrain lazy-loads submodules via importutils, invisible to
    # static analysis — enumerate the whole package.
    + collect_submodules("speechbrain"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not needed at runtime
        "tkinter",
        "matplotlib",
        "pytest",
        "pytest_cov",
        # coverage gets pulled in transitively by numba's coverage_support
        # module (numba is a transitive of pyannote). numba imports coverage
        # in a try/except ImportError, so excluding it lets numba fall back
        # to coverage_available=False. Including it instead is actively
        # harmful: coverage.__init__ runs realpath(getcwd()) at import
        # time, which raises FileNotFoundError if the daemon's spawn cwd
        # is unresolvable — exactly what happens when the Tauri shell
        # spawns the bundled sidecar.
        "coverage",
        "ruff",
        "pip",
        "setuptools",
        # torch was excluded until v0.2.0 (halved the bundle) because it
        # was only reachable via mlx_whisper's offline weight converter
        # and sentence-transformers. It now earns its ~520 MB: voice
        # identification (speechbrain ECAPA) hard-requires torch +
        # torchaudio, and with torch aboard sentence-transformers gives
        # the packaged daemon working semantic search instead of the
        # FTS5-only fallback. Re-exclude only if voice_id + embeddings
        # are both retired.
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="context-recall-daemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # BUNDLE (below) requires a windowed build; the flag has no effect on
    # the daemon's stdio, which launchd redirects via the LaunchAgent.
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="context-recall-daemon",
)

# Wrap the payload in a real .app bundle. macOS TCC KILLS any process
# that requests microphone access without an Info.plist carrying
# NSMicrophoneUsageDescription (observed 2026-07-07 as a launchd crash
# loop, last exit reason OS_REASON_TCC), and only a bundle can carry
# one. BUNDLE also produces the codesign-compliant Frameworks/Resources
# layout a hand-rolled wrapper does not.
app = BUNDLE(
    coll,
    name="Context Recall Daemon.app",
    icon=None,
    bundle_identifier="dev.jamiewhite.contextrecall.daemon",
    version="0.1.0",
    info_plist={
        "CFBundleName": "Context Recall Daemon",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": (
            "Context Recall records meeting audio (the system-audio "
            "loopback and your microphone) to transcribe and summarise "
            "your meetings. Audio is captured only while a meeting is "
            "detected or you press Record."
        ),
    },
)
