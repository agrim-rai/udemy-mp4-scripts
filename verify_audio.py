import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True)
    except Exception:
        print("Error: ffmpeg is not installed or not in PATH.")
        sys.exit(1)

def verify_audio_file(file_path):
    cmd = [
        'ffmpeg',
        '-t', '15',          # Check first 15 seconds of audio
        '-i', file_path,
        '-af', 'volumedetect',
        '-vn',               # Discard video stream
        '-f', 'null',
        '-'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        stderr = result.stderr or ""
        
        # Check if no audio stream exists
        if "Stream map '0:a' matches no streams" in stderr or "does not contain any stream" in stderr:
            return "NO_AUDIO", "No audio stream found", None
            
        # Parse volume detection statistics
        mean_volume_match = re.search(r'mean_volume:\s*([\d\.-]+)\s*dB', stderr)
        max_volume_match = re.search(r'max_volume:\s*([\d\.-]+)\s*dB', stderr)
        n_samples_matches = re.findall(r'n_samples:\s*(\d+)', stderr)
        
        if not mean_volume_match or not n_samples_matches:
            # Check if there is some other ffmpeg error
            if result.returncode != 0:
                err_msg = stderr.split('\n')[-2:]
                return "ERROR", f"ffmpeg error: {err_msg}", None
            return "NO_AUDIO", "No audio stream found", None
            
        mean_val = float(mean_volume_match.group(1))
        max_val = float(max_volume_match.group(1))
        n_samples = int(n_samples_matches[-1])  # Take the final count of samples

        
        if n_samples == 0:
            return "NO_AUDIO", "Audio stream exists but contains 0 samples", None
            
        # If mean volume is extremely low (e.g. -60 dB or lower), it is silent/mute
        # (Udemy videos typically have mean volumes of -12dB to -25dB)
        if mean_val < -55.0:
            return "SILENT", f"Audio is silent/mute (mean: {mean_val} dB, max: {max_val} dB)", mean_val
            
        return "PASS", f"Healthy (mean: {mean_val} dB)", mean_val
        
    except subprocess.TimeoutExpired:
        return "ERROR", "Verification timed out", None
    except Exception as e:
        return "ERROR", str(e), None

def main(output_dir="output"):
    check_ffmpeg()
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

    print(f"Found {len(video_files)} video files. Starting audio quality check...")
    
    no_audio = []
    silent = []
    errors = []
    passed = []
    
    # Process files concurrently
    max_workers = min(16, (os.cpu_count() or 4) * 2)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(tqdm(
            executor.map(lambda fp: (fp, verify_audio_file(fp)), video_files),
            total=len(video_files),
            desc="Checking audio streams"
        ))

    for fp, (status, detail, mean_val) in results:
        rel = os.path.relpath(fp, output_dir)
        if status == "PASS":
            passed.append((rel, detail))
        elif status == "NO_AUDIO":
            no_audio.append((rel, detail))
        elif status == "SILENT":
            silent.append((rel, detail))
        else:
            errors.append((rel, detail))

    print("\n" + "=" * 80)
    print("Audio Verification Summary:")
    print("=" * 80)
    print(f"Total videos checked     : {len(video_files)}")
    print(f"Healthy Audio (PASS)     : {len(passed)}")
    print(f"Silent/Mute Audio        : {len(silent)}")
    print(f"No Audio Track (MUTE)    : {len(no_audio)}")
    print(f"Processing Errors        : {len(errors)}")
    print("=" * 80)

    any_issues = silent or no_audio or errors
    
    if silent:
        print("\n[MUTE/SILENT DETECTED]:")
        for rel, detail in silent:
            print(f"  - {rel}\n    Reason: {detail}")
            
    if no_audio:
        print("\n[NO AUDIO TRACK DETECTED]:")
        for rel, detail in no_audio:
            print(f"  - {rel}\n    Reason: {detail}")
            
    if errors:
        print("\n[PROCESSING ERRORS]:")
        for rel, detail in errors:
            print(f"  - {rel}\n    Reason: {detail}")

    if any_issues:
        print("\nVerification finished: Audio issues found in some files.")
        sys.exit(1)
    else:
        print("\nAll videos have healthy audio tracks and are not muted!")
        sys.exit(0)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "output"
    main(target)
