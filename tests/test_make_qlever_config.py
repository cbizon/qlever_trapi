from make_qlever_config import (
    direct_cat_input_files_command,
    qleverfile_cat_input_files_command,
    render_index_command,
    render_qleverfile,
)


def test_render_qleverfile_includes_native_runtime_and_input_file():
    text = render_qleverfile(
        dataset_name="translator_kg",
        input_file="translator_kg.nt",
        port=8888,
        ui_port=8176,
        access_token="token",
    )
    assert "NAME         = translator_kg" in text
    assert "INPUT_FILES     = translator_kg.nt" in text
    assert "SYSTEM = native" in text
    assert "PORT         = 8888" in text
    assert "UI_PORT   = 8176" in text


def test_render_qleverfile_uses_zstdcat_for_compressed_input():
    text = render_qleverfile(
        dataset_name="translator_kg",
        input_file="translator_kg.nt.zst",
        port=8888,
        ui_port=8176,
        access_token="token",
    )
    assert "INPUT_FILES     = translator_kg.nt.zst" in text
    assert "CAT_INPUT_FILES = zstd -dc -- ${INPUT_FILES}" in text


def test_render_index_command_adds_cat_input_files_for_zstd():
    command = render_index_command("translator_kg", "translator_kg.nt.zst", "32G")
    assert "--input-files 'translator_kg.nt.zst'" in command
    assert "--cat-input-files 'zstd -dc -- translator_kg.nt.zst'" in command
    assert "--text-index from_literals" in command
    assert qleverfile_cat_input_files_command("translator_kg.nt.zst") == "zstd -dc -- ${INPUT_FILES}"
    assert direct_cat_input_files_command("translator_kg.nt.zst") == "zstd -dc -- translator_kg.nt.zst"
