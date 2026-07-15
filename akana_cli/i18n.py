"""Tiny i18n for the setup/CLI experience — English + Turkish.

The setup wizard asks for a language up front (or takes ``--lang``); every message
after that is emitted through :func:`t`. Other commands (``doctor``/``add``/…) pick
the language up from ``AKANA_LANGUAGE`` in ``.env`` (wired in ``main.py``), so the
whole CLI speaks one language once setup has recorded the choice.

Dependency-light on purpose: a plain dict, no external i18n lib, importable before
any dependency is installed (first-run bootstrap runs under the system Python).
"""

from __future__ import annotations

SUPPORTED = ("en", "tr")
_DEFAULT = "en"
_lang = _DEFAULT


def set_lang(lang: str | None) -> str:
    """Set the active CLI language; unknown/empty falls back to English. Returns it."""
    global _lang
    norm = (lang or "").strip().lower()
    _lang = norm if norm in SUPPORTED else _DEFAULT
    return _lang


def get_lang() -> str:
    return _lang


def t(key: str, /, *, default: str | None = None, **params: object) -> str:
    """Translate ``key`` for the active language; interpolate ``{name}`` params.

    Falls back to the English string, then ``default``, then the key itself — so a
    missing translation degrades to readable text instead of crashing setup.

    ``key`` is positional-only (``/``) so a string can use a ``{key}`` placeholder
    (``t("doctor.key_defined", key=...)``) without colliding with this parameter.
    """
    entry = _STRINGS.get(key)
    if entry is not None:
        s = entry.get(_lang) or entry.get("en") or default or key
    else:
        s = default if default is not None else key
    for name, value in params.items():
        s = s.replace("{" + name + "}", str(value))
    return s


#: key → {en, tr}. Grouped by area; keep the en text identical to the pre-i18n
#: wording so English behaviour is unchanged.
_STRINGS: dict[str, dict[str, str]] = {
    # ── language picker (shown bilingually, BEFORE a language is chosen) ──────
    "lang.prompt": {
        "en": "Select a language / Bir dil seçin:",
        "tr": "Select a language / Bir dil seçin:",
    },
    "lang.english": {"en": "English", "tr": "English (İngilizce)"},
    "lang.turkish": {"en": "Turkish (Türkçe)", "tr": "Türkçe"},
    "lang.choice": {"en": "Choice / Seçim", "tr": "Choice / Seçim"},

    # ── setup: banners / intro ───────────────────────────────────────────────
    "setup.title": {"en": "Akana — setup", "tr": "Akana — kurulum"},
    "setup.repo": {"en": "Repo", "tr": "Depo"},
    "setup.platform": {"en": "Platform", "tr": "Platform"},
    "setup.intro": {
        "en": "Sets up: Python deps → your model provider → optional add-ons. Ctrl+C to abort.",
        "tr": "Kurar: Python paketleri → model sağlayıcın → isteğe bağlı eklentiler. İptal için Ctrl+C.",
    },
    "setup.complete": {"en": "Setup complete", "tr": "Kurulum tamamlandı"},

    # ── setup: section headers ───────────────────────────────────────────────
    "setup.sec_python": {"en": "Python packages", "tr": "Python paketleri"},
    "setup.sec_choose": {"en": "Choose & install", "tr": "Seç & kur"},
    "setup.sec_configure": {"en": "Configure", "tr": "Yapılandır"},
    "setup.sec_health": {"en": "Health check", "tr": "Sağlık kontrolü"},

    # ── setup: env / python ──────────────────────────────────────────────────
    "setup.env_created": {
        "en": ".env created (from .env.example)",
        "tr": ".env oluşturuldu (.env.example'dan)",
    },
    "setup.env_example_missing": {"en": ".env.example not found", "tr": ".env.example bulunamadı"},
    "setup.py_required": {
        "en": "Python 3.11+ required — none found on PATH",
        "tr": "Python 3.11+ gerekli — PATH'te bulunamadı",
    },
    "setup.venv_creating": {"en": "Virtual environment ({py})", "tr": "Sanal ortam ({py})"},
    "setup.venv_present": {"en": "venv present", "tr": "venv mevcut"},
    "setup.venv_failed": {
        "en": "venv creation failed — on Debian/Ubuntu install the venv module first: "
        "sudo apt install -y python3-venv  (then re-run setup)",
        "tr": "venv oluşturulamadı — Debian/Ubuntu'da önce venv modülünü kur: "
        "sudo apt install -y python3-venv  (sonra kurulumu yeniden çalıştır)",
    },
    "setup.venv_repair": {
        "en": "Repairing virtual environment (removing venv/ for a clean rebuild)",
        "tr": "Sanal ortam onarılıyor (temiz yeniden kurulum için venv/ siliniyor)",
    },
    "setup.venv_repair_failed": {
        "en": "could not remove {path} ({err}) — delete it by hand, then re-run setup",
        "tr": "{path} silinemedi ({err}) — elle sil, sonra kurulumu yeniden çalıştır",
    },
    "setup.pip_bootstrap": {"en": "Bootstrapping pip (ensurepip)", "tr": "pip kuruluyor (ensurepip)"},
    "setup.pip_bootstrap_failed": {
        "en": "ensurepip failed — venv looks incomplete. Rebuild it: python akana.py setup --repair",
        "tr": "ensurepip başarısız — venv eksik görünüyor. Yeniden kur: python akana.py setup --repair",
    },
    "setup.pip_upgrade": {"en": "Upgrading pip", "tr": "pip güncelleniyor"},
    "setup.core_installing": {"en": "Installing core packages", "tr": "Çekirdek paketler kuruluyor"},
    "setup.core_failed": {"en": "core package install failed", "tr": "çekirdek paket kurulumu başarısız"},
    "setup.core_label": {"en": "core packages", "tr": "çekirdek paketler"},
    "setup.data_dirs": {"en": "Data directories", "tr": "Veri dizinleri"},
    "setup.data_dirs_skipped": {
        "en": "data-dir bootstrap skipped — they'll be created on first start",
        "tr": "veri dizini oluşturma atlandı — ilk başlangıçta oluşturulacak",
    },

    # ── setup: install progress (verbose) ────────────────────────────────────
    "setup.pkg_plan": {
        "en": "Installing {n} package(s): {names}",
        "tr": "{n} paket kuruluyor: {names}",
    },
    "setup.pkg_plan_more": {"en": "…and {n} more", "tr": "…ve {n} tane daha"},
    "setup.pkg_downloading": {"en": "downloading {pkg}", "tr": "indiriliyor: {pkg}"},
    "setup.pkg_installing": {"en": "installing {pkg}", "tr": "kuruluyor: {pkg}"},
    "setup.done_in": {"en": "{label} ready ({secs}s)", "tr": "{label} hazır ({secs}sn)"},

    # ── setup: extras / bridge ───────────────────────────────────────────────
    "setup.extra_installing": {"en": "Installing {label}", "tr": "{label} kuruluyor"},
    "setup.extra_ready": {"en": "{label} ready", "tr": "{label} hazır"},
    "setup.extra_failed": {
        "en": "{label} install failed — retry with: python akana.py add {label}",
        "tr": "{label} kurulumu başarısız — yeniden dene: python akana.py add {label}",
    },
    "setup.bridge_no_node": {
        "en": "node/npm not found — bridge skipped (required for the Cursor provider)",
        "tr": "node/npm bulunamadı — bridge atlandı (Cursor sağlayıcısı için gerekli)",
    },
    "setup.bridge_installing": {"en": "Installing Cursor bridge", "tr": "Cursor bridge kuruluyor"},
    "setup.bridge_ready": {"en": "Cursor bridge ready", "tr": "Cursor bridge hazır"},
    "setup.bridge_failed": {
        "en": "Cursor bridge install failed — in cursor_bridge/ run: npm install",
        "tr": "Cursor bridge kurulumu başarısız — cursor_bridge/ içinde çalıştır: npm install",
    },

    # ── setup: choose / configure ────────────────────────────────────────────
    "setup.choose_prompt": {
        "en": "Select what to install (providers + add-ons):",
        "tr": "Kurulacakları seç (sağlayıcılar + eklentiler):",
    },
    "setup.installed_tag": {"en": "installed", "tr": "kurulu"},
    "setup.all_selected_present": {
        "en": "Everything you selected is already installed.",
        "tr": "Seçtiğin her şey zaten kurulu.",
    },
    "setup.installing_n": {"en": "Installing {n} component(s)…", "tr": "{n} bileşen kuruluyor…"},
    "setup.enter_keys": {
        "en": "Enter API keys in Akana after launch — Settings → Identity:",
        "tr": "API anahtarlarını Akana açılınca gir — Ayarlar → Kimlik:",
    },
    "setup.active_provider": {"en": "Active provider: {id}", "tr": "Aktif sağlayıcı: {id}"},
    "setup.which_default": {
        "en": "Which provider should be active by default?",
        "tr": "Hangi sağlayıcı varsayılan olarak aktif olsun?",
    },
    "setup.token_prompt": {
        "en": "Enable phone / remote access? (auto-generates a secure key)",
        "tr": "Telefon / uzaktan erişim etkinleştirilsin mi? (güvenli bir anahtar otomatik üretilir)",
    },
    "setup.token_ask": {
        "en": "Paste your own key, or press Enter to use the generated one",
        "tr": "Kendi anahtarını yapıştır ya da üretileni kullanmak için Enter'a bas",
    },
    "setup.token_saved": {
        "en": "Phone access key (save it): {token}",
        "tr": "Telefon erişim anahtarı (kaydet): {token}",
    },
    "setup.provider_unconfigured": {
        "en": "{key} is empty — {provider} chat won't work until you add it (.env or Settings → Identity).",
        "tr": "{key} boş — {provider} sohbeti, ekleyene kadar çalışmaz (.env ya da Ayarlar → Kimlik).",
    },

    # ── setup: summary ───────────────────────────────────────────────────────
    "setup.no_provider_callout": {
        "en": "No model provider configured — chat will not work. Pick one in the web "
        "onboarding, or run: python akana.py add cursor|claude|gemini|openai|codex|ollama",
        "tr": "Model sağlayıcı yapılandırılmadı — sohbet çalışmaz. Web kurulumundan birini "
        "seç ya da çalıştır: python akana.py add cursor|claude|gemini|openai|codex|ollama",
    },
    "setup.install_errored": {
        "en": "{id} installed with errors — re-run: python akana.py add {id}",
        "tr": "{id} hatalarla kuruldu — yeniden çalıştır: python akana.py add {id}",
    },
    "setup.your_setup": {"en": "Your setup:", "tr": "Kurulumun:"},
    "setup.sum_provider": {"en": "provider", "tr": "sağlayıcı"},
    "setup.sum_installed": {"en": "installed", "tr": "kurulu"},
    "setup.sum_token": {"en": "remote token", "tr": "uzak anahtar"},
    "setup.sum_none_provider": {
        "en": "none (pick one in Settings → Identity)",
        "tr": "yok (Ayarlar → Kimlik'ten seç)",
    },
    "setup.sum_none": {"en": "none", "tr": "yok"},
    "setup.sum_token_set": {"en": "set", "tr": "ayarlı"},
    "setup.sum_token_local": {"en": "none (local-only)", "tr": "yok (yalnız yerel)"},
    "setup.start_hint": {"en": "Start:", "tr": "Başlat:"},
    "setup.stop_hint": {"en": "Stop:", "tr": "Durdur:"},
    "setup.browser_hint": {"en": "Browser:", "tr": "Tarayıcı:"},
    "setup.add_hint": {"en": "Add later:", "tr": "Sonra ekle:"},
    "setup.add_hint_tail": {
        "en": "(providers, voice, embeddings)",
        "tr": "(sağlayıcılar, ses, gömme vektörleri)",
    },

    # ── component labels (checklist rows) ────────────────────────────────────
    "comp.cursor": {
        "en": "Cursor — wide model catalog (Node bridge + API key)",
        "tr": "Cursor — geniş model kataloğu (Node bridge + API anahtarı)",
    },
    "comp.claude": {
        "en": "Claude — claude-code CLI + subscription",
        "tr": "Claude — claude-code CLI + abonelik",
    },
    "comp.gemini": {"en": "Gemini — Google API key", "tr": "Gemini — Google API anahtarı"},
    "comp.openai": {
        "en": "OpenAI — API key (no extra package)",
        "tr": "OpenAI — API anahtarı (ek paket yok)",
    },
    "comp.codex": {
        "en": "Codex — OpenAI codex CLI + ChatGPT subscription",
        "tr": "Codex — OpenAI codex CLI + ChatGPT aboneliği",
    },
    "comp.ollama": {"en": "Ollama — local models, no key", "tr": "Ollama — yerel modeller, anahtar yok"},
    "comp.embeddings": {
        "en": "Semantic memory recall (fastembed ONNX, ~220 MB, no GPU)",
        "tr": "Anlamsal hafıza hatırlama (fastembed ONNX, ~220 MB, GPU yok)",
    },
    "comp.voice-piper": {
        "en": "Piper TTS — offline speech output",
        "tr": "Piper TTS — çevrimdışı konuşma çıkışı",
    },
    "comp.voice-full": {
        "en": "Full voice — Piper TTS + Whisper STT + 'Hey Akana' wake word",
        "tr": "Tam ses — Piper TTS + Whisper STT + 'Hey Akana' uyandırma sözcüğü",
    },
    "comp.xtts": {
        "en": "XTTS-v2 — high-quality local TTS (TR + voice cloning), heavy",
        "tr": "XTTS-v2 — yüksek kaliteli yerel TTS (TR + ses klonlama), ağır",
    },

    # ── toolchain preflight ──────────────────────────────────────────────────
    "tool.checking": {"en": "Checking your toolchain…", "tr": "Araç zincirin kontrol ediliyor…"},
    "tool.node_ok": {"en": "Node.js {ver}", "tr": "Node.js {ver}"},
    "tool.node_old": {
        "en": "Node.js {ver} is too old — the Cursor bridge needs 18+",
        "tr": "Node.js {ver} çok eski — Cursor bridge 18+ ister",
    },
    "tool.node_missing": {
        "en": "Node.js not found — needed ONLY for the Cursor provider",
        "tr": "Node.js bulunamadı — YALNIZ Cursor sağlayıcısı için gerekli",
    },
    "tool.npm_missing": {"en": "npm not found (comes with Node.js)", "tr": "npm bulunamadı (Node.js ile gelir)"},
    "tool.claude_ok": {"en": "Claude CLI found", "tr": "Claude CLI bulundu"},
    "tool.claude_missing": {
        "en": "Claude CLI not found — needed ONLY for the Claude provider",
        "tr": "Claude CLI bulunamadı — YALNIZ Claude sağlayıcısı için gerekli",
    },
    "tool.node_install_hint": {
        "en": "Install Node.js 18+ to use the Cursor provider:",
        "tr": "Cursor sağlayıcısını kullanmak için Node.js 18+ kur:",
    },
    "tool.claude_install_hint": {
        "en": "Install the Claude CLI to use the Claude provider:",
        "tr": "Claude sağlayıcısını kullanmak için Claude CLI kur:",
    },
    "tool.claude_login_hint": {
        "en": "Then log in once — run:  claude  (or:  claude setup-token)",
        "tr": "Sonra bir kez giriş yap — çalıştır:  claude  (ya da:  claude setup-token)",
    },
    "tool.codex_login_hint": {
        "en": "Then log in once — run:  codex login  (ChatGPT sign-in, no API key)",
        "tr": "Sonra bir kez giriş yap — çalıştır:  codex login  (ChatGPT girişi, API anahtarı yok)",
    },
    "tool.claude_installing": {
        "en": "Installing the Claude CLI ({pkg})",
        "tr": "Claude CLI kuruluyor ({pkg})",
    },
    "tool.claude_installed": {"en": "Claude CLI installed", "tr": "Claude CLI kuruldu"},
    "tool.claude_install_failed": {
        "en": "Install failed (permissions?) — try: npm i -g {pkg}",
        "tr": "Kurulum başarısız (izinler?) — dene: npm i -g {pkg}",
    },
    "tool.npm_confirm": {
        "en": "Install {pkg} globally now (npm i -g)?",
        "tr": "{pkg} şimdi global kurulsun mu (npm i -g)?",
    },
    "tool.npm_install_later": {"en": "Install later: npm i -g {pkg}", "tr": "Sonra kur: npm i -g {pkg}"},
    "tool.npm_missing_for": {
        "en": "npm not found — install {pkg} manually, then run `{bin}`",
        "tr": "npm bulunamadı — {pkg} paketini elle kur, sonra `{bin}` çalıştır",
    },

    # ── add <component> ──────────────────────────────────────────────────────
    "add.banner": {"en": "Akana — add: {id}", "tr": "Akana — ekle: {id}"},
    "add.intro": {
        "en": "Add a provider or optional component:",
        "tr": "Bir sağlayıcı ya da isteğe bağlı bileşen ekle:",
    },
    "add.which": {"en": "Which component?", "tr": "Hangi bileşen?"},
    "add.installed": {"en": "installed", "tr": "kurulu"},
    "add.not_installed": {"en": "not installed", "tr": "kurulu değil"},
    "add.unknown": {"en": "Unknown component: {id}", "tr": "Bilinmeyen bileşen: {id}"},
    "add.available": {"en": "Available: {ids}", "tr": "Mevcut: {ids}"},
    "add.already_present": {
        "en": "{id} requirements already present — skipping.",
        "tr": "{id} gereksinimleri zaten mevcut — atlanıyor.",
    },
    "add.already_ready": {
        "en": "{id} is already installed and configured.",
        "tr": "{id} zaten kurulu ve yapılandırılmış.",
    },
    "add.rerun": {"en": "Re-run anyway?", "tr": "Yine de yeniden çalıştırılsın mı?"},
    "add.key_hint": {
        "en": "{key} not set — add it in Akana after launch: Settings → Identity ({url})",
        "tr": "{key} ayarlı değil — Akana açılınca ekle: Ayarlar → Kimlik ({url})",
    },
    "add.piper_voices": {"en": "Piper voice files", "tr": "Piper ses dosyaları"},
    "add.piper_choose": {
        "en": "Select which Piper voices to download (Enter = default TR + EN):",
        "tr": "İndirilecek Piper seslerini seç (Enter = varsayılan TR + EN):",
    },
    "add.piper_failed": {
        "en": "Piper download failed — check your network; try again later",
        "tr": "Piper indirmesi başarısız — ağını kontrol et; sonra tekrar dene",
    },
    "add.wake_active": {
        "en": "'Hey Akana' wake word is bundled and ON by default (no download needed).",
        "tr": "'Hey Akana' uyandırma sözcüğü pakette gelir ve varsayılan olarak AÇIK (indirme gerekmez).",
    },
    "add.whisper_prompt": {
        "en": "Whisper STT model (downloads on first use)",
        "tr": "Whisper STT modeli (ilk kullanımda indirilir)",
    },
    "add.ready": {"en": "{id} ready.", "tr": "{id} hazır."},
    "add.restart": {
        "en": "Restart Akana to apply — run: python akana.py stop  then: python akana.py start",
        "tr": "Uygulamak için Akana'yı yeniden başlat — çalıştır: python akana.py stop  sonra: python akana.py start",
    },
    "add.external_hint": {"en": "{id} must be installed separately.", "tr": "{id} ayrıca kurulmalı."},

    # ── io: prompts / controls ───────────────────────────────────────────────
    "io.secret_default_hint": {
        "en": "Enter = use generated key",
        "tr": "Enter = üretilen anahtarı kullan",
    },
    "io.choice": {"en": "Choice", "tr": "Seçim"},
    "io.invalid_pick": {"en": "Invalid — pick one of: {opts}", "tr": "Geçersiz — şunlardan birini seç: {opts}"},
    "io.checklist_controls": {
        "en": "[a]ll  [n]one  [Enter] = install selected  [q] = skip",
        "tr": "[a]=tümü  [n]=hiçbiri  [Enter] = seçilenleri kur  [q] = atla",
    },
    "io.toggle_num": {"en": "Toggle #", "tr": "Değiştir #"},
    "io.checklist_hint": {
        "en": "(enter a number, 'a', 'n', or Enter to confirm)",
        "tr": "(bir sayı, 'a', 'n' ya da onaylamak için Enter gir)",
    },
    "io.cancelled": {"en": "Cancelled — re-run anytime.", "tr": "İptal edildi — istediğin zaman yeniden çalıştır."},
    "io.cmd_failed": {
        "en": "Command failed (exit {code}). See the output above for details.",
        "tr": "Komut başarısız (çıkış {code}). Ayrıntılar için yukarıdaki çıktıya bak.",
    },

    # ── doctor ───────────────────────────────────────────────────────────────
    "doctor.title": {"en": "Akana — doctor", "tr": "Akana — doktor"},
    "doctor.py_sys": {"en": "System Python: {path}", "tr": "Sistem Python: {path}"},
    "doctor.py_missing": {"en": "Python 3.11+ not found (PATH)", "tr": "Python 3.11+ bulunamadı (PATH)"},
    "doctor.venv": {"en": "venv: {path}", "tr": "venv: {path}"},
    "doctor.venv_missing": {
        "en": "venv missing — first run: python akana.py setup",
        "tr": "venv yok — önce çalıştır: python akana.py setup",
    },
    "doctor.no_provider": {
        "en": "No LLM provider configured — set one in Settings, or: python akana.py add <provider>",
        "tr": "LLM sağlayıcı yapılandırılmadı — Ayarlar'dan seç ya da: python akana.py add <provider>",
    },
    "doctor.node_npm": {"en": "node + npm", "tr": "node + npm"},
    "doctor.node_missing_cursor": {
        "en": "node/npm not found — Cursor bridge will not work",
        "tr": "node/npm bulunamadı — Cursor bridge çalışmaz",
    },
    "doctor.bridge_ok": {"en": "cursor_bridge installed", "tr": "cursor_bridge kurulu"},
    "doctor.bridge_missing": {"en": "cursor_bridge npm install missing", "tr": "cursor_bridge npm install eksik"},
    "doctor.node_present": {"en": "node/npm present", "tr": "node/npm mevcut"},
    "doctor.node_not_required": {
        "en": "node/npm not required for {provider}",
        "tr": "node/npm {provider} için gerekli değil",
    },
    "doctor.env_missing": {"en": ".env missing", "tr": ".env yok"},
    "doctor.key_defined": {"en": "{key} defined", "tr": "{key} tanımlı"},
    "doctor.key_empty": {
        "en": "{key} empty (.env) — {provider} chat won't work",
        "tr": "{key} boş (.env) — {provider} sohbeti çalışmaz",
    },
    "doctor.cursor_reachable": {"en": "Cursor API reachable ({n} models)", "tr": "Cursor API erişilebilir ({n} model)"},
    "doctor.cursor_unreachable": {"en": "Cursor API unreachable: {err}", "tr": "Cursor API erişilemez: {err}"},
    "doctor.cursor_check_skipped": {"en": "Cursor API check skipped: {err}", "tr": "Cursor API kontrolü atlandı: {err}"},
    "doctor.claude_found": {"en": "claude CLI found", "tr": "claude CLI bulundu"},
    "doctor.claude_missing": {
        "en": "claude CLI not found — install @anthropic-ai/claude-code + run `claude` to log in",
        "tr": "claude CLI bulunamadı — @anthropic-ai/claude-code kur + giriş için `claude` çalıştır",
    },
    "doctor.ollama": {
        "en": "ollama provider (local) — ensure it is running",
        "tr": "ollama sağlayıcısı (yerel) — çalıştığından emin ol",
    },
    "doctor.provider_generic": {"en": "provider: {provider}", "tr": "sağlayıcı: {provider}"},
    "doctor.provider_pkg_missing": {
        "en": "{provider} provider package not installed (chat will fail)",
        "tr": "{provider} sağlayıcı paketi kurulu değil (sohbet çalışmaz)",
    },
    "doctor.port_ok": {"en": "Port {host}:{port} available", "tr": "Port {host}:{port} uygun"},
    "doctor.port_in_use": {
        "en": "Port {host}:{port} in use — server may already be running",
        "tr": "Port {host}:{port} kullanımda — sunucu zaten çalışıyor olabilir",
    },
    "doctor.data_dir": {"en": "Data directory: {path}", "tr": "Veri dizini: {path}"},
    "doctor.data_dir_missing": {
        "en": "Data directory does not exist yet: {path}",
        "tr": "Veri dizini henüz yok: {path}",
    },
    "doctor.optional_missing": {
        "en": "{label} not installed (optional, for {why})",
        "tr": "{label} kurulu değil (isteğe bağlı, {why} için)",
    },
    "doctor.add_hint": {"en": " — add: python akana.py add {id}", "tr": " — ekle: python akana.py add {id}"},
    "doctor.why_voice": {"en": "voice", "tr": "ses"},
    "doctor.why_vector": {"en": "semantic vector recall", "tr": "anlamsal vektör hatırlama"},
    "doctor.ready": {"en": "Looks ready — python akana.py start", "tr": "Hazır görünüyor — python akana.py start"},
    "doctor.issues": {"en": "{n} critical issue(s)", "tr": "{n} kritik sorun"},
    "doctor.mcp_banner": {
        "en": "MCP / Cursor-bridge spawn diagnostic",
        "tr": "MCP / Cursor bridge başlatma tanılaması",
    },
    "doctor.mcp_failed": {
        "en": "{n} MCP child(ren) failed the handshake",
        "tr": "{n} MCP alt süreci el sıkışmayı geçemedi",
    },
    "doctor.mcp_ok": {
        "en": "All MCP children passed the handshake",
        "tr": "Tüm MCP alt süreçleri el sıkışmayı geçti",
    },

    # ── start ────────────────────────────────────────────────────────────────
    "start.no_provider": {
        "en": "No LLM provider configured — pick one in Settings → Identity, or: python akana.py add <provider>",
        "tr": "LLM sağlayıcı yapılandırılmadı — Ayarlar → Kimlik'ten seç ya da: python akana.py add <provider>",
    },
    "start.key_missing": {
        "en": "{key} not in .env — if {provider} chat fails, set it (.env or Settings → Identity)",
        "tr": "{key} .env'de yok — {provider} sohbeti çalışmazsa ekle (.env ya da Ayarlar → Kimlik)",
    },
    "start.port_in_use": {
        "en": "Port {host}:{port} in use — server may already be running",
        "tr": "Port {host}:{port} kullanımda — sunucu zaten çalışıyor olabilir",
    },
    "start.stop_hint": {
        "en": "To stop it: python akana.py stop",
        "tr": "Durdurmak için: python akana.py stop",
    },
    "start.starting": {
        "en": "Starting server — http://{host}:{port}",
        "tr": "Sunucu başlatılıyor — http://{host}:{port}",
    },
    "start.ctrl_c": {"en": "Press Ctrl+C to stop", "tr": "Durdurmak için Ctrl+C'ye bas"},

    # ── add: verification / whisper sizes ────────────────────────────────────
    "add.verify_failed_bridge": {
        "en": "{id}: install reported success but the Cursor Node bridge is missing "
        "(@cursor/sdk) — Node/npm may not be installed. Retry with: python akana.py add {id}",
        "tr": "{id}: kurulum başarılı raporladı ama Cursor Node bridge eksik "
        "(@cursor/sdk) — Node/npm kurulu olmayabilir. Yeniden dene: python akana.py add {id}",
    },
    "add.verify_failed_pip": {
        "en": "{id}: install reported success but the package is NOT importable in the venv "
        "(it may have landed in user/site-packages). Retry with: python akana.py add {id}",
        "tr": "{id}: kurulum başarılı raporladı ama paket venv içinde içe aktarılamıyor "
        "(user/site-packages'a düşmüş olabilir). Yeniden dene: python akana.py add {id}",
    },
    "add.verify_failed_npm": {
        "en": "{id}: the CLI is not on PATH — the global npm install may have been "
        "skipped or failed (Node/npm may be missing). Retry with: python akana.py add {id}",
        "tr": "{id}: CLI PATH'te değil — global npm kurulumu atlanmış ya da başarısız "
        "olmuş olabilir (Node/npm eksik olabilir). Yeniden dene: python akana.py add {id}",
    },
    "add.oww_preinstall_failed": {
        "en": "openwakeword preinstall (--no-deps) failed — skipping the voice install "
        "to avoid a tflite-runtime resolver error. Retry with: python akana.py add voice-full",
        "tr": "openwakeword ön kurulumu (--no-deps) başarısız — tflite-runtime çözümleyici "
        "hatasından kaçınmak için ses kurulumu atlanıyor. Yeniden dene: python akana.py add voice-full",
    },
    "add.incomplete": {
        "en": "{id}: install did not complete — see the errors above.",
        "tr": "{id}: kurulum tamamlanmadı — yukarıdaki hatalara bak.",
    },
    "add.whisper.tiny": {
        "en": "tiny — fastest, least accurate (~75 MB)",
        "tr": "tiny — en hızlı, en az doğru (~75 MB)",
    },
    "add.whisper.base": {"en": "base — fast (~145 MB)", "tr": "base — hızlı (~145 MB)"},
    "add.whisper.small": {
        "en": "small — balanced (default, ~480 MB)",
        "tr": "small — dengeli (varsayılan, ~480 MB)",
    },
    "add.whisper.medium": {
        "en": "medium — most accurate, slower (~1.5 GB)",
        "tr": "medium — en doğru, daha yavaş (~1.5 GB)",
    },

    # ── component install hints / notes (external installers + first-use notes) ─
    "comp.cursor.hint": {
        "en": "Cursor needs Node.js + npm for the @cursor/sdk bridge.",
        "tr": "Cursor, @cursor/sdk bridge için Node.js + npm ister.",
    },
    "comp.claude.hint": {
        "en": "After install, run `claude` once to log in.",
        "tr": "Kurulumdan sonra giriş için bir kez `claude` çalıştır.",
    },
    "comp.ollama.hint": {
        "en": "Install from https://ollama.com, run `ollama serve`, then `ollama pull <model>`.",
        "tr": "https://ollama.com'dan kur, `ollama serve` çalıştır, sonra `ollama pull <model>`.",
    },
    "comp.embeddings.note": {
        "en": "The embedding model downloads on first recall (~220 MB).",
        "tr": "Gömme modeli ilk hatırlamada indirilir (~220 MB).",
    },
    "comp.xtts.note": {
        "en": "XTTS needs PyTorch (the CUDA wheel installs separately); the model "
        "downloads on first synthesis (~2 GB).",
        "tr": "XTTS, PyTorch ister (CUDA wheel ayrıca kurulur); model ilk "
        "sentezde indirilir (~2 GB).",
    },

    # ── Piper voice descriptions (per catalog entry) ─────────────────────────
    "voice.desc.tr_TR-dfki-medium": {"en": "Turkish — dfki (medium)", "tr": "Türkçe — dfki (orta)"},
    "voice.desc.tr_TR-fahrettin-medium": {
        "en": "Turkish — fahrettin (medium)",
        "tr": "Türkçe — fahrettin (orta)",
    },
    "voice.desc.en_US-amy-medium": {"en": "English (US) — amy (medium)", "tr": "İngilizce (ABD) — amy (orta)"},
    "voice.desc.en_US-lessac-medium": {
        "en": "English (US) — lessac (medium)",
        "tr": "İngilizce (ABD) — lessac (orta)",
    },
    "voice.desc.en_US-ryan-high": {"en": "English (US) — ryan (high)", "tr": "İngilizce (ABD) — ryan (yüksek)"},
    "voice.desc.en_GB-alba-medium": {
        "en": "English (GB) — alba (medium)",
        "tr": "İngilizce (İngiltere) — alba (orta)",
    },

    # ── smoke ────────────────────────────────────────────────────────────────
    "smoke.banner": {"en": "Akana — core smoke", "tr": "Akana — çekirdek duman testi"},
    "smoke.doctor_failed": {"en": "doctor failed", "tr": "doktor başarısız"},
    "smoke.venv_missing": {"en": "venv missing", "tr": "venv yok"},
    "smoke.running": {
        "en": "pytest (core smoke + health + conversations)",
        "tr": "pytest (çekirdek duman + sağlık + konuşmalar)",
    },
    "smoke.failed": {"en": "Core smoke tests failed", "tr": "Çekirdek duman testleri başarısız"},
    "smoke.passed": {"en": "Core smoke passed", "tr": "Çekirdek duman testi geçti"},

    # ── stop ─────────────────────────────────────────────────────────────────
    "stop.looking": {
        "en": "Looking for a process listening on port {host}:{port}…",
        "tr": "{host}:{port} portunu dinleyen bir süreç aranıyor…",
    },
    "stop.port_free": {
        "en": "Port {port} is free — the server does not appear to be running",
        "tr": "Port {port} boş — sunucu çalışmıyor görünüyor",
    },
    "stop.not_found_note": {
        "en": "Note: If no process is found, use Ctrl+C in the terminal or Task Manager.",
        "tr": "Not: Süreç bulunamazsa terminalde Ctrl+C ya da Görev Yöneticisi'ni kullan.",
    },
    "stop.stopping": {"en": "Stopping PID {pid}…", "tr": "PID {pid} durduruluyor…"},
    "stop.stopped_pid": {"en": "PID {pid} stopped", "tr": "PID {pid} durduruldu"},
    "stop.stop_failed_pid": {
        "en": "PID {pid} could not be stopped — terminate it manually",
        "tr": "PID {pid} durdurulamadı — elle sonlandır",
    },
    "stop.stopped": {"en": "Server stopped", "tr": "Sunucu durduruldu"},

    # ── reset-memory ─────────────────────────────────────────────────────────
    "reset.resetting": {"en": "Resetting memory: {path}", "tr": "Hafıza sıfırlanıyor: {path}"},
    "reset.preserved_note": {
        "en": "(The conversation archive and episodic.db are left untouched.)",
        "tr": "(Konuşma arşivi ve episodic.db'ye dokunulmaz.)",
    },
    "reset.server_running": {
        "en": "The Akana server may be running — 'python akana.py stop' is recommended before resetting.",
        "tr": "Akana sunucusu çalışıyor olabilir — sıfırlamadan önce 'python akana.py stop' önerilir.",
    },
    "reset.nothing": {"en": "No files to reset.", "tr": "Sıfırlanacak dosya yok."},
    "reset.restart_hint": {
        "en": "Restart the server: python akana.py start",
        "tr": "Sunucuyu yeniden başlat: python akana.py start",
    },
    "reset.browser_hint": {
        "en": "In the browser: Memory Studio → Refresh all.",
        "tr": "Tarayıcıda: Hafıza Stüdyosu → Tümünü yenile.",
    },
    "reset.db_failed": {
        "en": "could not reset memory.db: {exc}",
        "tr": "memory.db sıfırlanamadı: {exc}",
    },
    "reset.db_failed_hint": {
        "en": "Is the server running? Stop it first: python akana.py stop",
        "tr": "Sunucu çalışıyor mu? Önce durdur: python akana.py stop",
    },
    "reset.cleared": {
        "en": "staging/semantic/graph/vector cleared in {path}",
        "tr": "staging/semantic/graph/vector temizlendi: {path}",
    },

    # ── backup / restore ──────────────────────────────────────────────────────
    "backup.no_data_dir": {
        "en": "No data dir at {path} — nothing to back up.",
        "tr": "{path} konumunda veri dizini yok — yedeklenecek bir şey yok.",
    },
    "backup.backing_up": {"en": "Backing up: {path}", "tr": "Yedekleniyor: {path}"},
    "backup.server_running": {
        "en": "The Akana server appears to be running — this is safe (SQLite uses the online backup API), just proceeding.",
        "tr": "Akana sunucusu çalışıyor görünüyor — bu güvenli (SQLite çevrimiçi yedek API'sini kullanır), devam ediliyor.",
    },
    "backup.vault_key_warning": {
        "en": "WARNING: --include-vault-key bundles your MASTER KEY — the archive can then decrypt ALL your secrets in plaintext. Store it as carefully as a password.",
        "tr": "UYARI: --include-vault-key ANA ANAHTARINI da paketler — arşiv artık TÜM sırlarını düz metin olarak çözebilir. Onu bir parola kadar dikkatli sakla.",
    },
    "backup.vault_key_missing": {
        "en": "--include-vault-key requested but no key file found (key may be in env/keyring) — archive stays ciphertext-only.",
        "tr": "--include-vault-key istendi ama anahtar dosyası bulunamadı (anahtar env/keyring'de olabilir) — arşiv yalnız şifreli-metin kalır.",
    },
    "backup.db_raw_copy": {
        "en": "{name} is not a valid SQLite DB ({exc}) — copied as-is.",
        "tr": "{name} geçerli bir SQLite veritabanı değil ({exc}) — olduğu gibi kopyalandı.",
    },
    "backup.failed": {"en": "Backup failed: {exc}", "tr": "Yedekleme başarısız: {exc}"},
    "backup.done": {
        "en": "Backed up {count} files → {path} ({mb} MB)",
        "tr": "{count} dosya yedeklendi → {path} ({mb} MB)",
    },
    "backup.ciphertext_note": {
        "en": "Secrets are encrypted with a key stored OUTSIDE this archive — restore on the SAME machine, or use --include-vault-key for another machine.",
        "tr": "Sırlar bu arşivin DIŞINDA saklanan bir anahtarla şifreli — AYNI makinede geri yükle, ya da başka makine için --include-vault-key kullan.",
    },
    "restore.no_archive": {"en": "No such archive: {path}", "tr": "Böyle bir arşiv yok: {path}"},
    "restore.server_running": {
        "en": "The Akana server is running — stop it first ('python akana.py stop') before restoring.",
        "tr": "Akana sunucusu çalışıyor — geri yüklemeden önce durdur ('python akana.py stop').",
    },
    "restore.restoring": {"en": "Restoring {src} → {dst}", "tr": "Geri yükleniyor {src} → {dst}"},
    "restore.bad_archive": {
        "en": "Archive has no manifest — not an Akana backup.",
        "tr": "Arşivde manifest yok — bir Akana yedeği değil.",
    },
    "restore.hash_mismatch": {
        "en": "Integrity check failed (corrupt archive): {files}",
        "tr": "Bütünlük kontrolü başarısız (bozuk arşiv): {files}",
    },
    "restore.unlisted": {
        "en": "Archive contains files not in its manifest (tampered): {files}",
        "tr": "Arşiv, manifestinde olmayan dosyalar içeriyor (kurcalanmış): {files}",
    },
    "restore.exists": {
        "en": "{path} already has data — pass --force to move it aside and restore.",
        "tr": "{path} zaten veri içeriyor — kenara alıp geri yüklemek için --force geç.",
    },
    "restore.moved_aside": {"en": "Existing data moved to {path}", "tr": "Mevcut veri {path} konumuna taşındı"},
    "restore.failed": {"en": "Restore failed: {exc}", "tr": "Geri yükleme başarısız: {exc}"},
    "restore.vault_key_written": {
        "en": "Master key restored to {path}",
        "tr": "Ana anahtar {path} konumuna geri yüklendi",
    },
    "restore.done": {"en": "Restored to {path}", "tr": "{path} konumuna geri yüklendi"},
    "restore.restart_hint": {
        "en": "Start Akana: python akana.py start",
        "tr": "Akana'yı başlat: python akana.py start",
    },

    # ── setup: doctor-preflight fallback + OS install-hint tails ──────────────
    "setup.doctor_skipped": {
        "en": "doctor preflight skipped ({exc}). Re-run: python akana.py doctor",
        "tr": "doktor ön kontrolü atlandı ({exc}). Yeniden çalıştır: python akana.py doctor",
    },
    "setup.py_hint_download": {
        "en": "…or download:      https://www.python.org/downloads/",
        "tr": "…ya da indir:       https://www.python.org/downloads/",
    },
    "setup.py_hint_download_win": {
        "en": "…or download (tick 'Add to PATH'): https://www.python.org/downloads/",
        "tr": "…ya da indir ('Add to PATH' işaretle): https://www.python.org/downloads/",
    },
    "tool.node_hint_distro": {
        "en": "https://nodejs.org/  — or your distro's nodejs 18+ package",
        "tr": "https://nodejs.org/  — ya da dağıtımının nodejs 18+ paketi",
    },
    "tool.node_hint_or": {"en": "(or https://nodejs.org/)", "tr": "(ya da https://nodejs.org/)"},

    # ── io: yes/no answer hints ──────────────────────────────────────────────
    "io.yn_default_yes": {"en": "Y/n", "tr": "E/h"},
    "io.yn_default_no": {"en": "y/N", "tr": "e/H"},

    # ── argparse help / descriptions ─────────────────────────────────────────
    "cli.help.description": {"en": "Akana — setup and server", "tr": "Akana — kurulum ve sunucu"},
    "cli.help.setup": {"en": "Setup (default: interactive)", "tr": "Kurulum (varsayılan: etkileşimli)"},
    "cli.help.setup_yes": {"en": "No prompts (voice: none)", "tr": "Soru sorma (ses: yok)"},
    "cli.help.setup_voice": {"en": "Voice mode (with --yes)", "tr": "Ses modu (--yes ile)"},
    "cli.help.setup_repair": {
        "en": "Rebuild the virtual environment from scratch (fixes a corrupt venv)",
        "tr": "Sanal ortamı sıfırdan yeniden kur (bozuk venv'i onarır)",
    },
    "cli.help.setup_lang": {
        "en": "Interface language (en/tr). Interactive setup asks; --yes defaults to en.",
        "tr": "Arayüz dili (en/tr). Etkileşimli kurulum sorar; --yes varsayılan olarak en.",
    },
    "cli.help.add": {
        "en": "Install an optional component later (voice, embeddings, a provider)",
        "tr": "Sonradan isteğe bağlı bir bileşen kur (ses, gömme, bir sağlayıcı)",
    },
    "cli.help.add_component": {
        "en": "Component to add (omit for a menu): ",
        "tr": "Eklenecek bileşen (menü için boş bırak): ",
    },
    "cli.help.smoke": {"en": "Core smoke (doctor + pytest)", "tr": "Çekirdek duman (doktor + pytest)"},
    "cli.help.start": {"en": "Start the server (uvicorn)", "tr": "Sunucuyu başlat (uvicorn)"},
    "cli.help.stop": {"en": "Stop the server (by port)", "tr": "Sunucuyu durdur (porta göre)"},
    "cli.help.doctor": {
        "en": "Pre-flight check (Python, venv, key, port)",
        "tr": "Ön kontrol (Python, venv, anahtar, port)",
    },
    "cli.help.doctor_mcp": {
        "en": "Also spawn the MCP/Cursor-bridge children and health-check their handshake",
        "tr": "MCP/Cursor bridge alt süreçlerini de başlat ve el sıkışmalarını sağlık-kontrol et",
    },
    "cli.help.test": {"en": "Unit tests (pytest)", "tr": "Birim testleri (pytest)"},
    "cli.help.ship": {"en": "Pack a portable tarball", "tr": "Taşınabilir bir tarball paketle"},
    "cli.help.ship_out": {
        "en": "Output directory (default: repo root)",
        "tr": "Çıktı dizini (varsayılan: depo kökü)",
    },
    "cli.help.reset_memory": {
        "en": "Delete Inbox / staging / semantic / graph caches (conversations preserved)",
        "tr": "Gelen Kutusu / staging / semantic / graph önbelleklerini sil (konuşmalar korunur)",
    },
    "cli.help.backup": {
        "en": "Snapshot the data dir (~/.akana) to a .tar.gz",
        "tr": "Veri dizinini (~/.akana) bir .tar.gz'ye yedekle",
    },
    "cli.help.backup_out": {
        "en": "Output file or directory (default: current dir)",
        "tr": "Çıktı dosyası veya dizini (varsayılan: geçerli dizin)",
    },
    "cli.help.backup_voices": {
        "en": "Also back up voices/ (large, re-downloadable models)",
        "tr": "voices/ dizinini de yedekle (büyük, yeniden indirilebilir modeller)",
    },
    "cli.help.backup_key": {
        "en": "Also bundle the vault master key (cross-machine restore — sensitive!)",
        "tr": "Kasa ana anahtarını da paketle (makineler arası geri yükleme — hassas!)",
    },
    "cli.help.restore": {
        "en": "Restore the data dir from a backup .tar.gz (stop the server first)",
        "tr": "Veri dizinini bir yedek .tar.gz'den geri yükle (önce sunucuyu durdur)",
    },
    "cli.help.restore_archive": {
        "en": "Path to the backup .tar.gz",
        "tr": "Yedek .tar.gz'nin yolu",
    },
    "cli.help.restore_force": {
        "en": "Move an existing data dir aside instead of refusing",
        "tr": "Mevcut veri dizinini reddetmek yerine kenara al",
    },

    # ── .env file ────────────────────────────────────────────────────────────
    "env.not_utf8": {
        "en": ".env is not UTF-8 ({error}) — re-save {path} as UTF-8. "
        "(A common cause on Windows is PowerShell `>` redirection, which writes UTF-16.)",
        "tr": ".env UTF-8 değil ({error}) — {path} dosyasını UTF-8 olarak yeniden kaydet. "
        "(Windows'ta sık görülen bir neden, UTF-16 yazan PowerShell `>` yönlendirmesidir.)",
    },
}
