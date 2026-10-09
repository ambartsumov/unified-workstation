# PyInstaller build description — one definition for Linux, macOS and Windows.
#   pip install ".[package]" && pyinstaller --noconfirm packaging/unified-workstation.spec
# Output: dist/UnifiedWorkstation/ (Linux, Windows) or dist/Unified Workstation.app (macOS).
# Signing is not done here: packaging/macos/sign.sh and the release workflow do it, with keys
# that live only in the CI secret store.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from suw import product  # noqa: E402

datas = [
    (str(ROOT / "suw" / "resources"), "suw/resources"),
    (str(ROOT / "suw" / "app" / "static"), "suw/app/static"),
    (str(ROOT / "suw" / "cloud" / "scripts"), "suw/cloud/scripts"),
    (str(ROOT / "suw" / "core" / "probe.sh"), "suw/core"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
analysis = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["suw.app.entry", "suw.app.launch", "suw.app.backend", "suw.app.server", "suw.cli.main", "suw.daemon.suwd", "suw.app.demo", "suw.platform.linux", "suw.platform.macos", "suw.platform.windows"],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
icon = {"win32": ROOT / "packaging" / "windows" / "app.ico", "darwin": ROOT / "packaging" / "macos" / "app.icns"}.get(sys.platform)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="unified-workstation",
    console=False,
    icon=str(icon) if icon and icon.exists() else None,
    version=str(ROOT / "build" / "windows-version.txt") if sys.platform == "win32" and (ROOT / "build" / "windows-version.txt").exists() else None,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(ROOT / "packaging" / "macos" / "entitlements.plist") if sys.platform == "darwin" else None,
)
bundle = COLLECT(exe, analysis.binaries, analysis.datas, name="UnifiedWorkstation")
if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name=f"{product.NAME}.app",
        bundle_identifier=product.BUNDLE_ID,
        version=product.VERSION,
        icon=str(icon) if icon and icon.exists() else None,
        info_plist={
            "CFBundleDisplayName": product.NAME,
            "CFBundleShortVersionString": product.VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSLocalNetworkUsageDescription": "Finds and connects to your other paired computers on this network.",
            "NSHumanReadableCopyright": f"{product.VENDOR}. {product.LICENSE} licence.",
        },
    )
