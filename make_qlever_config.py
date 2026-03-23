#!/usr/bin/env python3
import argparse
import secrets
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write a Qleverfile and print index/start commands for an RDF dataset."
    )
    parser.add_argument(
        "--dataset-name",
        default="translator_kg",
        help="Dataset name used by qlever-control.",
    )
    parser.add_argument(
        "--input-file",
        default="translator_kg.nt",
        help="RDF input file to index.",
    )
    parser.add_argument(
        "--qleverfile",
        default="Qleverfile",
        help="Path to the Qleverfile to write.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="Server port for qlever start.",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=8176,
        help="UI port for qlever ui.",
    )
    parser.add_argument(
        "--memory",
        default="32G",
        help="Value for `qlever index --stxxl-memory`.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing Qleverfile.",
    )
    return parser.parse_args()


def render_qleverfile(
    dataset_name: str,
    input_file: str,
    port: int,
    ui_port: int,
    access_token: str,
) -> str:
    cat_input_files = qleverfile_cat_input_files_command(input_file)
    return f"""# Generated for indexing a KGX-derived RDF dataset with qlever-control.
[data]
NAME         = {dataset_name}
GET_DATA_CMD =
DESCRIPTION  = RDF converted from {input_file}

[index]
INPUT_FILES     = {input_file}
CAT_INPUT_FILES = {cat_input_files}
SETTINGS_JSON   = {{ "num-triples-per-batch": 1000000 }}

[server]
PORT         = {port}
ACCESS_TOKEN = {access_token}

[runtime]
SYSTEM = native

[ui]
UI_PORT   = {ui_port}
UI_CONFIG = default
"""


def qleverfile_cat_input_files_command(input_file: str) -> str:
    if input_file.endswith(".zst"):
        return "zstd -dc -- ${INPUT_FILES}"
    return "cat ${INPUT_FILES}"


def direct_cat_input_files_command(input_file: str) -> str:
    if input_file.endswith(".zst"):
        return f"zstd -dc -- {input_file}"
    return f"cat {input_file}"


def render_index_command(dataset_name: str, input_file: str, memory: str) -> str:
    command = [
        "qlever index",
        "--system native",
        f"--name {dataset_name}",
        "--format nt",
        f"--input-files '{input_file}'",
    ]
    if input_file.endswith(".zst"):
        command.append(f"--cat-input-files '{direct_cat_input_files_command(input_file)}'")
    command.extend(
        [
            "--parallel-parsing false",
            "--text-index from_literals",
            f"--stxxl-memory {memory}",
        ]
    )
    return " ".join(command)


def main() -> None:
    args = parse_args()
    qleverfile = Path(args.qleverfile)
    if qleverfile.exists() and not args.overwrite:
        raise SystemExit(f"{qleverfile} already exists; pass --overwrite to replace it")

    access_token = secrets.token_urlsafe(18)
    qleverfile.write_text(
        render_qleverfile(
            dataset_name=args.dataset_name,
            input_file=args.input_file,
            port=args.port,
            ui_port=args.ui_port,
            access_token=access_token,
        ),
        encoding="utf-8",
    )

    print(f"Wrote {qleverfile}")
    print()
    print("Index command:")
    print(render_index_command(args.dataset_name, args.input_file, args.memory))
    print()
    print("Start command:")
    print(f"qlever start --system native --name {args.dataset_name}")
    print()
    print("UI command:")
    print(f"qlever ui --qleverfile {qleverfile}")


if __name__ == "__main__":
    main()
