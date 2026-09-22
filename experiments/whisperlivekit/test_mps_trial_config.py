"""Device selection must stay explicit and survive CLI/config conversion."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get("WLK_MPS_SOURCE", str(ROOT / "artifacts/whisperlivekit-mps-20260915/source")))
from whisperlivekit.config import WhisperLiveKitConfig
from whisperlivekit.parse_args import parse_args


@pytest.mark.parametrize("device,dtype", [("cpu", "float32"), ("mps", "float32"), ("mps", "float16")])
def test_cli_device_dtype_reach_engine_configuration(device, dtype):
    args = parse_args(["--backend", "mlx-whisper", "--backend-policy", "simulstreaming",
                       "--decoder-device", device, "--decoder-dtype", dtype])
    config = WhisperLiveKitConfig.from_kwargs(**vars(args))
    assert config.decoder_device == device
    assert config.decoder_dtype == dtype
    assert config.backend_policy == "simulstreaming"


@pytest.mark.parametrize("options", [{"decoder_device": "gpu"}, {"decoder_dtype": "float64"}])
def test_invalid_configuration_cannot_silently_fall_back(options):
    with pytest.raises(ValueError, match="Invalid decoder"):
        WhisperLiveKitConfig(**options)


def test_default_preserves_upstream_automatic_selection():
    config = WhisperLiveKitConfig.from_kwargs(**vars(parse_args([])))
    assert config.decoder_device == "auto" and config.decoder_dtype == "float32"
