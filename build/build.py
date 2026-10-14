from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import fontTools

from font_merge import merge_fonts


ROOT = Path(__file__).resolve().parents[1]
INSTANCE_PATH = ROOT / "build" / "instance.json"
CHECKPOINT_PATH = ROOT / "parr0t-release-checkpoint.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def digest_json(value: Any) -> str:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(data)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RuntimeError(f"json_object_required:{path.name}")
    return value


def verify_file(path: Path, *, size: int, digest: str, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing_input:{label}")
    if path.stat().st_size != size:
        raise RuntimeError(f"size_mismatch:{label}")
    if sha256_file(path) != digest:
        raise RuntimeError(f"sha256_mismatch:{label}")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def run_checked(command: list[str], *, env: dict[str, str], label: str) -> None:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"subprocess_failed:{label}:exit_{result.returncode}")


def prepare_output(path: Path) -> Path:
    output = path.resolve()
    if output == ROOT or ROOT in output.parents:
        raise RuntimeError("output_must_not_be_inside_repository")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("output_directory_must_be_empty")
    output.mkdir(parents=True, exist_ok=True)
    return output


def validate_sdk(parr0t_repo: Path, instance: dict[str, Any]) -> Path:
    sdk = instance["sdk"]
    if not (parr0t_repo / ".git").exists():
        raise RuntimeError("parr0t_repository_missing")
    if git(parr0t_repo, "rev-parse", "HEAD") != sdk["commit"]:
        raise RuntimeError("parr0t_commit_mismatch")
    if git(parr0t_repo, "status", "--porcelain"):
        raise RuntimeError("parr0t_worktree_not_clean")
    profile = parr0t_repo / "profiles" / sdk["branch"] / "profile.json"
    verify_file(
        profile,
        size=profile.stat().st_size if profile.is_file() else -1,
        digest=sdk["adapter_fingerprint"],
        label="sdk_profile",
    )
    return profile.parent


def validate_checkpoint(
    checkpoint: dict[str, Any], instance: dict[str, Any], parr0t_repo: Path
) -> bytes:
    sys.path.insert(0, str(parr0t_repo / "src"))
    from game_l10n.release_checkpoint import canonical_checkpoint_bytes

    contract = instance["checkpoint"]
    verify_file(
        CHECKPOINT_PATH,
        size=contract["bytes"],
        digest=contract["sha256"],
        label="release_checkpoint",
    )
    validated, canonical = canonical_checkpoint_bytes(
        CHECKPOINT_PATH,
        expected_patch_id=instance["patch_id"],
        expected_release_id=instance["release_id"],
    )
    if canonical != CHECKPOINT_PATH.read_bytes() or validated != checkpoint:
        raise RuntimeError("checkpoint_not_canonical")
    if checkpoint["sdk"] != {
        "branch": instance["sdk"]["branch"],
        "commit": instance["sdk"]["commit"],
        "adapter_fingerprint": instance["sdk"]["adapter_fingerprint"],
    }:
        raise RuntimeError("checkpoint_sdk_binding_mismatch")
    return canonical


def validate_tools(
    repak: Path, retoc: Path, noto_font: Path, instance: dict[str, Any]
) -> None:
    expected_fonttools = instance["tools"]["fonttools"]
    if fontTools.__version__ != expected_fonttools:
        raise RuntimeError(
            f"fonttools_version_mismatch:expected_{expected_fonttools}"
        )
    for name, path in (("repak", repak), ("retoc", retoc)):
        contract = instance["tools"][name]
        verify_file(
            path,
            size=contract["bytes"],
            digest=contract["sha256"],
            label=name,
        )
    font = instance["font_source"]
    verify_file(
        noto_font,
        size=font["bytes"],
        digest=font["sha256"],
        label="noto_font",
    )


def validate_game(game_root: Path, instance: dict[str, Any]) -> None:
    if not game_root.is_dir():
        raise RuntimeError("game_root_missing")
    for item in instance["game"]["source_bindings"]:
        verify_file(
            game_root / Path(item["path"]),
            size=item["bytes"],
            digest=item["sha256"],
            label=f"game:{item['path']}",
        )


def validate_segments(
    segments_path: Path, checkpoint: dict[str, Any]
) -> list[dict[str, Any]]:
    inventory = checkpoint["source_inventory"]
    verify_file(
        segments_path,
        size=segments_path.stat().st_size if segments_path.is_file() else -1,
        digest=inventory["fingerprint"],
        label="source_inventory",
    )
    rows = [
        json.loads(line)
        for line in segments_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    entries = checkpoint["entries"]
    if len(rows) != inventory["target_count"] or len(rows) != len(entries):
        raise RuntimeError("source_inventory_count_mismatch")
    for row, entry in zip(rows, entries, strict=True):
        if str(row.get("id", "")) != entry["id"]:
            raise RuntimeError("source_inventory_order_mismatch")
        source = row.get("source")
        if not isinstance(source, str):
            raise RuntimeError("source_inventory_text_missing")
        if sha256_bytes(source.encode("utf-8")) != entry["source_sha256"]:
            raise RuntimeError("source_inventory_digest_mismatch")
    return rows


def write_reviewed(checkpoint: dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in checkpoint["entries"]:
            row = {
                "id": entry["id"],
                "text": entry["target"],
                "status": entry["status"],
            }
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build_package(
    *,
    output: Path,
    game_root: Path,
    parr0t_repo: Path,
    profile_root: Path,
    repak: Path,
    retoc: Path,
    noto_font: Path,
    instance: dict[str, Any],
    checkpoint: dict[str, Any],
    checkpoint_bytes: bytes,
) -> dict[str, Any]:
    extraction = output / "extraction"
    env = os.environ.copy()
    env.update(
        {
            "GAME_L10N_WORKDIR": str(output),
            "GAME_L10N_REPAK": str(repak),
            "GAME_L10N_RETOC": str(retoc),
            "GAME_L10N_SOURCE_LOCALE": "en",
            "GAME_L10N_TARGET_LOCALE": "ko",
            "GAME_L10N_UNREAL_PAK": str(
                game_root
                / next(
                    item["path"]
                    for item in instance["game"]["source_bindings"]
                    if item["path"].casefold().endswith(".pak")
                )
            ),
            "GAME_L10N_UNREAL_UTOC": str(
                game_root
                / next(
                    item["path"]
                    for item in instance["game"]["source_bindings"]
                    if item["path"].casefold().endswith(".utoc")
                )
            ),
            # repak 0.2.3 compresses entries in parallel and writes them in
            # completion order. One worker preserves the already-sorted input
            # order and makes the PAK byte-for-byte reproducible.
            "RAYON_NUM_THREADS": "1",
        }
    )
    run_checked(
        [sys.executable, str(profile_root / "tools" / "unpack.py"), str(game_root), str(extraction)],
        env=env,
        label="sdk_unpack",
    )
    summary = load_json(extraction / "summary.json")
    if summary.get("target_locale_existing") is not False:
        raise RuntimeError("game_already_contains_korean_locale")
    validate_segments(extraction / "segments.jsonl", checkpoint)

    reviewed = output / "reviewed-from-checkpoint.jsonl"
    write_reviewed(checkpoint, reviewed)

    font_root = (
        extraction
        / "loose"
        / "BarrelRollProd"
        / "Content"
        / "BarrelRoll"
        / "Art"
        / "Font"
    )
    fonts_output = output / "overlays" / "fonts"
    for item in instance["font_outputs"]:
        verify_file(
            font_root / Path(item["base"]),
            size=item["base_bytes"],
            digest=item["base_sha256"],
            label=f"base_font:{item['base']}",
        )
    font_report = merge_fonts(
        base_root=font_root,
        output_root=fonts_output,
        source_font=noto_font,
        checkpoint=checkpoint,
    )
    if font_report["gate"] != "pass":
        raise RuntimeError("font_merge_gate_failed")
    for item in instance["font_outputs"]:
        verify_file(
            fonts_output / item["output"],
            size=item["output_bytes"],
            digest=item["output_sha256"],
            label=f"merged_font:{item['output']}",
        )
    (output / "font-merge-report.json").write_text(
        json.dumps(font_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    config = instance["configuration"]
    source_config = extraction / "loose" / Path(config["source_path"])
    verify_file(
        source_config,
        size=config["source_bytes"],
        digest=config["source_sha256"],
        label="DefaultGame.ini:source",
    )
    config_output = output / "overlays" / "config" / "DefaultGame.ini"
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config_output.write_bytes(
        source_config.read_bytes() + config["append_utf8"].encode("utf-8")
    )
    verify_file(
        config_output,
        size=config["output_bytes"],
        digest=config["output_sha256"],
        label="DefaultGame.ini:output",
    )

    pack = instance["pack"]
    overlay_destinations = [
        "BarrelRollProd/Content/BarrelRoll/Art/Font/Barbaro/Barbaro.ufont",
        "BarrelRollProd/Content/BarrelRoll/Art/Font/Barbaro/Barbaro_Punta.ufont",
        "BarrelRollProd/Content/BarrelRoll/Art/Font/DIN/D-DIN.ufont",
        "BarrelRollProd/Content/BarrelRoll/Art/Font/DIN/D-DIN-Bold.ufont",
    ]
    request = {
        "schema": "parr0t.unreal-5.4.pack/v1",
        "extraction_root": str(extraction),
        "reviewed_path": str(reviewed),
        "project_name": pack["project_name"],
        "localization_target": pack["localization_target"],
        "target_locale": pack["target_locale"],
        "pak_name": pack["pak_name"],
        "path_hash_seed": pack["path_hash_seed"],
        "compression": pack["compression"],
        "overlays": [
            {
                "source": str(fonts_output / item["output"]),
                "destination": destination,
            }
            for item, destination in zip(
                instance["font_outputs"], overlay_destinations, strict=True
            )
        ]
        + [
            {
                "source": str(config_output),
                "destination": "BarrelRollProd/Config/DefaultGame.ini",
            }
        ],
    }
    request_path = output / "pack-request.json"
    request_path.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    pack_output = output / "pack"
    run_checked(
        [sys.executable, str(profile_root / "tools" / "pack.py"), str(request_path), str(pack_output)],
        env=env,
        label="sdk_pack",
    )
    patch_pak = pack_output / pack["pak_name"]
    verify_file(
        patch_pak,
        size=pack["expected_bytes"],
        digest=pack["expected_sha256"],
        label="patch_pak",
    )

    payload = output / "payload"
    patch_destination = (
        payload
        / "v1_1_0"
        / "BarrelRollProd"
        / "Content"
        / "Paks"
        / pack["pak_name"]
    )
    patch_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(patch_pak, patch_destination)
    notice_root = payload / "v1_1_0" / "Parr0t-Korean-Patch"
    notice_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "OFL-1.1.txt", notice_root / "OFL-1.1.txt")
    shutil.copy2(
        ROOT / "THIRD-PARTY-NOTICES.md",
        notice_root / "THIRD-PARTY-NOTICES.md",
    )

    bindings = [
        {
            "path": f"v1_1_0/{item['path']}",
            "bytes": item["bytes"],
            "sha256": item["sha256"],
            "error_code": "E_CUSTOM_BARREL_ROLL_BUILD",
        }
        for item in instance["game"]["source_bindings"]
    ]
    if digest_json(bindings) != checkpoint["game_builds"][0]["binding_fingerprint"]:
        raise RuntimeError("game_binding_fingerprint_mismatch")
    package_spec = {
        "patch_id": instance["patch_id"],
        "release_id": instance["release_id"],
        "display_name": instance["display_name"],
        "checkpoint_file": "parr0t-release-checkpoint.json",
        "replace_files": [],
        "custom_errors": [
            {
                "internal_code": "E_CUSTOM_BARREL_ROLL_BUILD",
                "public_code": "PARR0T-BARREL-E001",
                "message": "이 패치는 설치된 게임 빌드와 호환되지 않습니다.",
                "resolution": "Steam에서 게임을 최신 상태로 만든 뒤 해당 버전을 지원하는 패치를 사용해 주세요.",
            }
        ],
        "steam": {
            "app_id": instance["game"]["steam_app_id"],
            "install_dir": instance["game"]["steam_install_dir"],
            "required_files": [item["path"] for item in bindings],
            "source_bindings": bindings,
        },
    }
    package_work = output / "package-spec"
    package_work.mkdir(parents=True, exist_ok=True)
    spec_path = package_work / "steam-package.json"
    spec_path.write_text(
        json.dumps(package_spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (package_work / "parr0t-release-checkpoint.json").write_bytes(checkpoint_bytes)

    from game_l10n.steam import build_steam_package

    package_contract = instance["package"]
    archive = output / package_contract["filename"]
    package_report = build_steam_package(payload, spec_path, archive)
    verify_file(
        archive,
        size=package_contract["expected_bytes"],
        digest=package_contract["expected_sha256"],
        label="release_zip",
    )
    with zipfile.ZipFile(archive, "r") as package_zip:
        manifest_bytes = package_zip.read("parr0t-patch-manifest.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    if len(manifest_bytes) != package_contract["manifest_bytes"]:
        raise RuntimeError("manifest_size_mismatch")
    if sha256_bytes(manifest_bytes) != package_contract["manifest_sha256"]:
        raise RuntimeError("manifest_sha256_mismatch")
    if manifest.get("artifact_fingerprint") != package_contract["artifact_fingerprint"]:
        raise RuntimeError("artifact_fingerprint_mismatch")
    checksum = output / package_contract["checksum_filename"]
    checksum.write_bytes(
        (
            f"{package_contract['expected_sha256']}  "
            f"{package_contract['filename']}\n"
        ).encode("ascii")
    )
    verify_file(
        checksum,
        size=package_contract["checksum_bytes"],
        digest=package_contract["checksum_sha256"],
        label="release_checksum",
    )
    return {
        "status": "reproduced",
        "release_id": instance["release_id"],
        "checkpoint_sha256": instance["checkpoint"]["sha256"],
        "source_targets": checkpoint["source_inventory"]["target_count"],
        "reviewed": sum(
            entry["status"] == "reviewed" for entry in checkpoint["entries"]
        ),
        "source_preserved": sum(
            entry["status"] == "source_preserved"
            for entry in checkpoint["entries"]
        ),
        "pak_sha256": pack["expected_sha256"],
        "zip_sha256": package_contract["expected_sha256"],
        "checksum_sha256": package_contract["checksum_sha256"],
        "manifest_sha256": package_contract["manifest_sha256"],
        "artifact_fingerprint": package_contract["artifact_fingerprint"],
        "installer_contract": package_report.get("installer_contract"),
        "output": str(archive),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce the Barrel Roll Korean R5 release without modifying the game."
    )
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--parr0t-repo", type=Path, required=True)
    parser.add_argument("--noto-font", type=Path, required=True)
    parser.add_argument("--repak", type=Path, required=True)
    parser.add_argument("--retoc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = prepare_output(args.output)
    instance = load_json(INSTANCE_PATH)
    checkpoint = load_json(CHECKPOINT_PATH)
    parr0t_repo = args.parr0t_repo.resolve()
    game_root = args.game_root.resolve()
    repak = args.repak.resolve()
    retoc = args.retoc.resolve()
    noto_font = args.noto_font.resolve()

    profile_root = validate_sdk(parr0t_repo, instance)
    checkpoint_bytes = validate_checkpoint(checkpoint, instance, parr0t_repo)
    validate_tools(repak, retoc, noto_font, instance)
    validate_game(game_root, instance)
    result = build_package(
        output=output,
        game_root=game_root,
        parr0t_repo=parr0t_repo,
        profile_root=profile_root,
        repak=repak,
        retoc=retoc,
        noto_font=noto_font,
        instance=instance,
        checkpoint=checkpoint,
        checkpoint_bytes=checkpoint_bytes,
    )
    result_path = output / "build-result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
