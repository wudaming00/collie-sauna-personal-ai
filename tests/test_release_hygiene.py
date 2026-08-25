"""Release packaging invariants that must hold even on a maintainer's live machine."""

import base64
from fnmatch import fnmatchcase
import hashlib
import json
from pathlib import Path
import re
import subprocess

import harness
from harness.update import WINDOWS_PUBLISHER_CN


ROOT = Path(__file__).resolve().parents[1]


def _table_strings(text: str, heading: str) -> list[str]:
    body = text.split(heading, 1)[1].split("\n[", 1)[0]
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return re.findall(r'"([^"]+)"', body)


def _workflow_job(text: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\s*\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match is not None, f"release workflow is missing the {name!r} job"
    return match.group("body")


def test_release_versions_stay_aligned_across_core_changelog_and_examples():
    core_version = harness.__version__
    assert tuple(map(int, core_version.split("-", 1)[0].split("."))) >= (0, 21, 23), (
        "the rebuilt product must not regress behind the restored 0.21.23 release line"
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    changelog_match = re.search(
        r"(?m)^## v(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)(?:\s|$)",
        changelog,
    )
    assert changelog_match is not None, "CHANGELOG.md has no top-level '## v<version>' entry"
    changelog_version = changelog_match.group("version")

    manifest_path = ROOT / "examples" / "extensions" / "release-helper" / "collie-extension.json"
    assert manifest_path.is_file(), f"example extension manifest is missing: {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    minimum_collie = manifest.get("collie", {}).get("min_version")
    assert minimum_collie, f"{manifest_path} must declare collie.min_version"

    assert changelog_version == core_version, (
        f"release version drift: harness.__version__ is {core_version!r}, "
        f"but the first CHANGELOG.md release is {changelog_version!r}"
    )
    assert minimum_collie == core_version, (
        f"example extension compatibility drift: harness.__version__ is {core_version!r}, "
        f"but {manifest_path} requires collie.min_version {minimum_collie!r}"
    )


def test_vscode_readme_package_example_matches_extension_version():
    package_path = ROOT / "vscode-collie" / "package.json"
    readme_path = ROOT / "vscode-collie" / "README.md"
    package_version = json.loads(package_path.read_text(encoding="utf-8")).get("version")
    assert package_version, f"{package_path} must declare a version"

    readme = readme_path.read_text(encoding="utf-8")
    documented_versions = set(re.findall(
        r"\bcollie-([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)\.vsix\b",
        readme,
        flags=re.IGNORECASE,
    ))
    assert documented_versions, f"{readme_path} must include a versioned collie-<version>.vsix example"
    assert documented_versions == {package_version}, (
        f"VS Code packaging docs drift: package.json is {package_version!r}, "
        f"but README.md references {sorted(documented_versions)!r}"
    )
    assert package_path.with_name("LICENSE").is_file(), "the VSIX must carry its MIT license"
    repository = json.loads(package_path.read_text(encoding="utf-8")).get("repository", {})
    assert repository.get("url") == "https://github.com/colliehq/collie.git"


def test_python_release_metadata_is_current_and_data_packages_are_explicit():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools>=77"]' in config
    assert 'license = "MIT"' in config and 'license-files = ["LICENSE"]' in config
    for package in ("harness.browser_ext", "harness.wallpaper", "harness.webui"):
        assert f'"{package}"' in config, f"data package {package} must be explicit"
    assert '"harness.oauth_ext"' not in config


def test_release_workflow_packages_and_publishes_vscode_vsix():
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    installer_job = _workflow_job(workflow, "installer")
    vscode_job = _workflow_job(workflow, "vscode")
    release_job = _workflow_job(workflow, "release")

    assert "Get-AuthenticodeSignature -LiteralPath installer/Output/Collie-Setup.exe" in installer_job, (
        "the Windows release job must inspect the signer's certificate, not only any valid chain"
    )
    assert "X509NameType]::SimpleName" in installer_job, (
        "the Windows release job must extract the Authenticode publisher name"
    )
    publisher_match = re.search(r'\$publisher\s+-cne\s+"(?P<publisher>[^"]+)"', installer_job)
    assert publisher_match is not None, (
        "the Windows release job must require an exact Authenticode publisher"
    )
    assert publisher_match.group("publisher") == WINDOWS_PUBLISHER_CN, (
        f"signing publisher drift: the release workflow requires "
        f"{publisher_match.group('publisher')!r}, but Collie's updater accepts "
        f"{WINDOWS_PUBLISHER_CN!r}"
    )
    assert re.search(
        r"(?m)^\s*run:\s*npx\s+--yes\s+@vscode/vsce\s+package\s+--out\s+\.\./Collie-VSCode\.vsix\s*$",
        vscode_job,
    ), "the vscode release job must package ../Collie-VSCode.vsix with @vscode/vsce"
    assert re.search(r"(?m)^\s*name:\s*vscode-extension\s*$", vscode_job), (
        "the vscode release job must upload an artifact named 'vscode-extension'"
    )
    assert re.search(r"(?m)^\s*path:\s*Collie-VSCode\.vsix\s*$", vscode_job), (
        "the vscode release job must upload Collie-VSCode.vsix"
    )
    assert re.search(r"(?m)^\s*needs:\s*\[[^\]]*\bvscode\b[^\]]*\]\s*$", release_job), (
        "the GitHub release job must depend on the vscode packaging job"
    )
    assert re.search(
        r"(?m)^\s*artifacts/vscode-extension/Collie-VSCode\.vsix\s*$",
        release_job,
    ), "the GitHub release files must publish Collie-VSCode.vsix from its artifact"


def test_release_wheel_gate_requires_sdk_and_rejects_retired_oauth_proxy():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    wheel_job = _workflow_job(workflow, "wheel")
    assert "harness/claude_agent_sdk.py" in wheel_job
    assert "harness/claude_agent_worker.py" in wheel_job
    assert '"oauth_proxy" in name' in wheel_job
    assert '"/oauth_ext/" in name' in wheel_job


def test_release_gate_runs_on_all_platforms_and_smokes_the_built_wheel():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    quality = _workflow_job(workflow, "quality-gate")
    wheel = _workflow_job(workflow, "wheel")

    assert "runs-on: ${{ matrix.os }}" in quality
    assert "fail-fast: false" in quality
    for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert os_name in quality, f"release quality gate does not include {os_name}"
    assert '{ os: ubuntu-latest, python: "3.10" }' in quality
    assert "python-version: ${{ matrix.python }}" in quality
    assert 'shell: bash' in quality
    assert 'if [ "$RUNNER_OS" = "Linux" ]' in quality

    for job in ("wheel", "installer", "dmg", "vscode"):
        assert "needs: quality-gate" in _workflow_job(workflow, job)

    assert 'python -m venv "$RUNNER_TEMP/collie-wheel-smoke"' in wheel
    assert '"$SMOKE_PY" -m pip install --no-deps dist/*.whl' in wheel
    assert 'cd "$RUNNER_TEMP"' in wheel
    assert '"$SMOKE_PY" -I -' in wheel
    assert 'metadata.version("collie-harness")' in wheel
    assert 'resources.files("harness").joinpath("webui")' in wheel
    assert '"$RUNNER_TEMP/collie-wheel-smoke/bin/collie" --version' in wheel


def test_python_floor_and_windows_venv_are_one_documented_contract():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    run_all = (ROOT / "tests" / "run_all.sh").read_text(encoding="utf-8")
    landing = (ROOT / "landing" / "index.html").read_text(encoding="utf-8")
    landing_draft = (ROOT / "landing" / "index.draft.html").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.10"' in config
    assert '{ os: ubuntu-latest, python: "3.10" }' in ci
    assert "python-version: ${{ matrix.python }}" in ci
    for page in (landing, landing_draft):
        assert "Python 3.10+" in page and "Python 3.12+" not in page
    assert "Python 3.10 or newer" in install
    assert ".venv/Scripts/python.exe" in run_all
    assert run_all.index(".venv/bin/python") < run_all.index("for c in python3 python")
    assert run_all.index(".venv/Scripts/python.exe") < run_all.index("for c in python3 python")


def test_mac_payload_is_exactly_pinned_and_verified_before_extraction():
    payload = (ROOT / "installer" / "build_mac_payload.sh").read_text(encoding="utf-8")
    mac = (ROOT / "installer" / "build_mac.sh").read_text(encoding="utf-8")
    expected_digests = {
        "4572133a5542f306b9bdb155da5800f9e38950cd0a98d469b832ce256fe299ea",
        "1a94c83264731e9603fbea78e57e7ca8f20e7d91eb866627ac2304621b0f6f1f",
    }

    assert 'PYTHON_VERSION="3.12.14"' in payload
    assert 'PBS_RELEASE="20260814"' in payload
    assert "api.github.com/repos/astral-sh/python-build-standalone/releases/latest" not in payload
    assert ('releases/download/${PBS_RELEASE}/${ASSET}' in payload and
            'cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-install_only.tar.gz' in payload)
    assert set(re.findall(r'PBS_SHA256="([0-9a-f]{64})"', payload)) == expected_digests
    assert 'shasum -a 256 "$path"' in payload
    assert payload.index('verify_sha256 "$TARBALL"') < payload.index('tar -xzf "$TARBALL"')
    assert payload.index('verify_sha256 "$TARBALL.part"') < payload.index('mv "$TARBALL.part"')
    assert '<key>LSMinimumSystemVersion</key>        <string>12.0</string>' in mac


def test_docs_and_landing_match_the_work_queue_mac_and_ios_product():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    desktop = (ROOT / "docs" / "desktop.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    platforms = (ROOT / "docs" / "PLATFORMS.md").read_text(encoding="utf-8")
    ios_docs = (ROOT / "relay" / "IOS_APP.md").read_text(encoding="utf-8")
    landing = (ROOT / "landing" / "index.html").read_text(encoding="utf-8")
    landing_draft = (ROOT / "landing" / "index.draft.html").read_text(encoding="utf-8")
    ask = (ROOT / "landing" / "functions" / "api" / "chat.js").read_text(encoding="utf-8")

    for text in (readme, desktop):
        assert "Work queue" in text
        lowered = text.lower()
        assert "needs you" in lowered and "open missions" in lowered and "recent outcomes" in lowered
    for text in (readme, install, platforms):
        assert "Homebrew" in text
        assert "published yet" in text
    installer_docs = (ROOT / "installer" / "README.md").read_text(encoding="utf-8")
    assert "not published yet" in installer_docs
    brew_release = (ROOT / "installer" / "brew_release.sh").read_text(encoding="utf-8")
    assert "brew install wudaming00/collie/collie" in brew_release

    assert 'data-os="ios"' in landing and 'id="pane-ios"' in landing
    ios_pane = landing.split('id="pane-ios"', 1)[1].split('id="pane-linux"', 1)[0]
    assert "companion" in ios_pane.lower()
    assert "Collie-arm64.dmg" not in ios_pane
    ios_detection = 'if(/iphone|ipad|ipod/.test(p)||(p.indexOf("mac")>-1&&navigator.maxTouchPoints>1))return"ios";'
    mac_detection = 'if(p.indexOf("mac")>-1)return"mac";'
    assert landing.index(ios_detection) < landing.index(mac_detection)
    assert 'if(platform==="ios")' in landing
    assert 'data-os="ios"' in landing_draft and 'if(platform==="ios")' in landing_draft
    assert "never offer an iPhone visitor the macOS DMG" in ask
    assert "iOS is not a target for the macOS DMG" in ios_docs


def test_package_data_never_captures_credentials_or_removed_oauth_proxy():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    payload_builder = (ROOT / "installer" / "build_payload.ps1").read_text(encoding="utf-8")
    patterns = _table_strings(config, "[tool.setuptools.package-data]")
    excluded = _table_strings(config, "[tool.setuptools.exclude-package-data]")

    assert "browser_ext/*" not in patterns
    assert not any(fnmatchcase("browser_ext/token.txt", pattern) for pattern in patterns)
    assert {"browser_ext/token.txt", "browser_ext/auth.js", "browser_ext/*.txt"} <= set(excluded)

    required = (
        "browser_ext/background.js",
        "browser_ext/manifest.json",
        "browser_ext/icon128.png",
    )
    for relative in required:
        assert (ROOT / "harness" / relative).is_file(), relative
        assert any(fnmatchcase(relative, pattern) for pattern in patterns), relative

    assert not any(pattern.startswith("oauth_ext/") for pattern in patterns)
    removed = (
        "oauth_proxy.py",
        "oauth_ext/pi-oauth-proxy.js",
        "oauth_ext/opencode.jsonc",
    )
    for relative in removed:
        assert not (ROOT / "harness" / relative).exists(), relative
        assert relative not in payload_builder


def test_installer_upgrade_cleanup_is_targeted_to_owned_runtime_packages():
    iss = (ROOT / "installer" / "collie.iss").read_text(encoding="utf-8")
    section = iss.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]
    delete_lines = [line.strip() for line in section.splitlines()
                    if line.lstrip().startswith("Type:")]

    assert len(delete_lines) == 4
    assert any("site-packages\\harness\"" in line for line in delete_lines)
    assert any("collie_harness-*.dist-info" in line for line in delete_lines)
    assert any("site-packages\\pip\"" in line for line in delete_lines)
    assert any("pip-*.dist-info" in line for line in delete_lines)
    assert all('Name: "{app}\\python\\Lib\\site-packages\\' in line
               for line in delete_lines)
    assert all(".collie" not in line.lower() and "{user" not in line.lower()
               for line in delete_lines)
    assert not any(line.endswith('site-packages\"') for line in delete_lines)

    # InstallDelete cannot reconstruct files it removed. The complete previous runtime is renamed
    # out of the overlay path and restored on every non-committed Setup exit.
    assert ".collie-upgrade-backup-python" in iss
    assert "RenameFile(pythonDir, UpgradeBackupDir)" in iss
    assert "procedure RestoreUpgradeBackup" in iss
    assert "procedure DeinitializeSetup" in iss
    assert "RestoreUpgradeBackup;" in iss
    assert "-m harness.supervisor run" in iss.split("procedure RestoreUpgradeBackup", 1)[1]
    assert "if CurStep = ssDone" in iss
    assert "InstallCommitted := True" in iss

    # A silent upgrade has no language-page choice and must not touch the user's settings file.
    # Only first install seeds LANG; upgrades preserve provider/model and all other preferences.
    run_section = iss.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    language_entry = run_section.split('Parameters: "{code:AppLangParam}"', 1)[1].split("\n\n", 1)[0]
    assert "Check: ShouldApplyAppLanguage" in language_entry
    assert "function ShouldApplyAppLanguage: Boolean" in iss
    assert "Result := not UpgradeBackupActive" in iss
    # Defense in depth: snapshot the entire merge-safe settings file for an upgrade and restore it
    # before supervisor children start as well as at successful/failed Setup termination.
    assert "settings.json.collie-upgrade-backup" not in iss  # path is derived, never duplicated
    assert "UpgradeSettingsBackup := UpgradeSettingsPath + '.collie-upgrade-backup'" in iss
    assert "procedure RestoreUpgradeSettings(KeepBackup: Boolean)" in iss
    assert "BeforeInstall: RestoreUpgradeSettingsBeforeSupervisor" in run_section
    assert "if CurStep = ssPostInstall then" in iss
    assert "RestoreUpgradeSettings(False);" in iss


def test_payload_build_fails_closed_and_verifies_code_metadata_and_assets():
    script = (ROOT / "installer" / "build_payload.ps1").read_text(encoding="utf-8")

    for label in (
        'Assert-NativeExit "bootstrap pip"',
        'Assert-NativeExit "install payload build dependencies"',
        'Assert-NativeExit "install Collie into payload"',
        'Assert-NativeExit "verify bundled Collie"',
        'Assert-NativeExit "verify bundled pip"',
    ):
        assert label in script
    assert 'metadata.version("collie-harness")' in script
    assert "payload version mismatch" in script
    assert "exactly one Collie dist-info" in script
    assert "private browser credential leaked" in script
    assert '("browser_ext/token.txt", "browser_ext/auth.js")' in script
    assert "collie-payload-verify-" in script
    assert "Set-Content -LiteralPath $verifyPath" in script
    assert '$ver = & (Join-Path $py "python.exe") -c $verify' not in script
    assert '"harness.supervisor", "harness.automations", "harness.ops"' in script
    assert "refusing to remove a path outside the payload runtime" in script
    assert "refusing to remove a non-generated repository path" in script
    assert 'Remove-RepoBuildArtifact (Join-Path $repo "build")' in script
    assert 'Remove-RepoBuildArtifact (Join-Path $repo "collie_harness.egg-info")' in script
    assert "4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3" in script
    assert "FB24E693BAB954209A063D90953621412CCAD4A500905A726286E038F508DDF6" in script
    assert "raw.githubusercontent.com/pypa/get-pip/" in script
    assert "Assert-FileSha256 $zip $PyEmbedSha256" in script
    assert "Assert-FileSha256 $getpip $getPipSha256" in script
    assert ('Assert-AuthenticodePublisher (Join-Path $py "python.exe") '
            '"Python Software Foundation"') in script
    assert 'Assert-AuthenticodePublisher $wv "Microsoft Corporation"' in script
    assert 'pip install --upgrade --no-build-isolation --no-warn-script-location' in script


def test_formal_installer_payloads_include_claude_agent_sdk_by_default():
    windows = (ROOT / "installer" / "build_payload.ps1").read_text(encoding="utf-8")
    mac_payload = (ROOT / "installer" / "build_mac_payload.sh").read_text(
        encoding="utf-8")
    mac = (ROOT / "installer" / "build_mac.sh").read_text(encoding="utf-8")

    assert '"$repo[local,remote,claude]"' in windows
    assert '"claude_agent_sdk"' in windows
    assert 'EXTRAS="${3:-local,tui,desktop,remote,claude}"' in mac_payload
    assert 'EXTRAS="local,tui,desktop,remote,claude"' in mac
    assert '"claude": ["claude_agent_sdk"]' in mac
    assert 'build_mac_payload.sh "$APP" "$ARCH" "$EXTRAS"' in mac


def test_top_level_installer_build_checks_every_native_generator():
    script = (ROOT / "installer" / "build.ps1").read_text(encoding="utf-8")
    assert "branding-art generation failed" in script
    assert "language-data generation failed" in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "iscc failed" }' in script


def test_installer_has_one_owner_for_slack_recovery():
    iss = (ROOT / "installer" / "collie.iss").read_text(encoding="utf-8")
    assert "harness.supervisor run" in iss
    assert "glob.glob(os.path.expanduser('~/.collie/slack-*.pyw'))" not in iss
    assert "Slack listeners are discovered and adopted by the supervisor" in iss


def test_landing_build_derives_csp_hashes_from_exact_shipped_scripts():
    landing = ROOT / "landing"
    subprocess.run(["node", "build.mjs"], cwd=landing, check=True,
                   capture_output=True, text=True)
    html = (landing / "dist" / "index.html").read_text(encoding="utf-8")
    headers = (landing / "dist" / "_headers").read_text(encoding="utf-8")
    scripts = [body for attrs, body in re.findall(
        r"<script\b([^>]*)>([\s\S]*?)</script>", html, flags=re.IGNORECASE)
        if not re.search(r"\bsrc\s*=", attrs, flags=re.IGNORECASE)]
    expected = {
        "sha256-" + base64.b64encode(hashlib.sha256(
            script.replace("\r\n", "\n").replace("\r", "\n").encode()).digest()).decode()
        for script in scripts
    }
    actual = set(re.findall(r"sha256-[A-Za-z0-9+/]+={0,2}", headers))

    assert scripts and actual == expected
    assert "__COLLIE_INLINE_SCRIPT_HASHES__" not in headers
    assert "Strict-Transport-Security: max-age=31536000" in headers


def test_landing_rate_limit_uses_secret_keyed_daily_bucket_and_fails_closed():
    script = (ROOT / "landing" / "functions" / "api" / "chat.js").read_text(encoding="utf-8")
    assert "RATE_LIMIT_SALT" in script
    assert 'utf8Length(salt) < 32' in script
    assert 'crypto.subtle.importKey(' in script
    assert 'crypto.subtle.sign(' in script
    assert 'idFromName(bucket)' in script
    assert 'idFromName(`${day}:${ip}`)' not in script
    assert 'request.headers.get("CF-Connecting-IP") || "unknown"' not in script
