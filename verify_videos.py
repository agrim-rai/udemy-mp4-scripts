import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

def check_ffprobe():
    try:
        subprocess.run(['ffprobe', '-version'], capture_output=True)
    except Exception:
        print("Error: ffprobe is not installed or not in PATH.")
        sys.exit(1)

def verify_file(file_path):
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return False, result.stderr.strip() or "ffprobe error"
        
        duration_str = result.stdout.strip()
        # Ensure we got a valid number back for duration
        float(duration_str)
        return True, None
    except subprocess.TimeoutExpired:
        return False, "Verification timed out"
    except ValueError:
        return False, f"Invalid duration output: {duration_str}"
    except Exception as e:
        return False, str(e)

def main(output_dir="output"):
    check_ffprobe()
    if not os.path.exists(output_dir):
        print(f"Error: Directory '{output_dir}' does not exist.")
        sys.exit(1)

    print(f"Scanning '{output_dir}' for video files...")
    video_files = []
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.lower().endswith('.mp4'):
                video_files.append(os.path.join(root, f))

    if not video_files:
        print("No video files found to verify.")
        sys.exit(0)

    print(f"Found {len(video_files)} video files. Starting integrity verification...")
    
    corrupt_files = []
    
    # Verify using ThreadPoolExecutor for speed
    max_workers = min(16, (os.cpu_count() or 4) * 2)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Wrap in tqdm progress bar
        results = list(tqdm(
            executor.map(lambda fp: (fp, verify_file(fp)), video_files),
            total=len(video_files),
            desc="Verifying videos"
        ))

    for fp, (is_valid, error_msg) in results:
        if not is_valid:
            corrupt_files.append((fp, error_msg))

    print("\n" + "=" * 80)
    print("Verification Summary:")
    print("=" * 80)
    print(f"Total videos checked : {len(video_files)}")
    print(f"Valid videos         : {len(video_files) - len(corrupt_files)}")
    print(f"Corrupt/Invalid      : {len(corrupt_files)}")
    print("=" * 80)

    if corrupt_files:
        print("\nThe following files are CORRUPT or UNOPENABLE:")
        for fp, err in corrupt_files:
            rel = os.path.relpath(fp, output_dir)
            print(f"  - {rel}\n    Reason: {err}")
        sys.exit(1)
    else:
        print("\nAll videos are healthy, fully openable, and have valid formats!")
        sys.exit(0)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "output"
    main(target)
