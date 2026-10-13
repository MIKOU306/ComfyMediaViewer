"""Optional engine detection without importing the server or loading model bytes."""
import argparse
import json
import os
from pathlib import Path
import sys


def has_models(app_dir, user_data):
    app_dir, user_data = Path(app_dir).absolute(), Path(user_data).absolute()
    candidates = [app_dir/'models/nsfw', user_data/'models/nsfw']
    selector = app_dir/'portable.flag'
    if selector.is_file():
        data = app_dir/'cmv-data'
        try:
            value = json.loads(selector.read_text('utf-8'))
            raw = value.get('dir') if isinstance(value, dict) else None
            if isinstance(raw, str) and raw.strip():
                data = Path(raw.strip())
                if not data.is_absolute():
                    data = app_dir/data
        except (OSError, ValueError):
            pass
        candidates.append(data/'models/nsfw')
    return any(folder.is_dir() and any(folder.glob('*.onnx')) for folder in candidates)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--app-dir')
    args, _ = parser.parse_known_args()
    app = os.environ.get('CMV_EXE_DIR') or args.app_dir or Path(__file__).parent
    user = Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ComfyMediaViewer'
    sys.exit(0 if has_models(app,user) else 1)
