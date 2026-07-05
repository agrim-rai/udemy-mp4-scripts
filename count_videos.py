import os
import sys

def count_videos(output_dir="output"):
    if not os.path.exists(output_dir):
        print(f"Error: Directory '{output_dir}' does not exist.")
        return

    total_count = 0
    chapter_counts = {}

    # Walk through the output directory
    for root, dirs, files in os.walk(output_dir):
        # Count mp4 files in current directory
        mp4_files = [f for f in files if f.lower().endswith('.mp4')]
        if mp4_files:
            relative_dir = os.path.relpath(root, output_dir)
            chapter_counts[relative_dir] = len(mp4_files)
            total_count += len(mp4_files)

    print("=" * 60)
    print(f"{'Chapter Folder':<45} | {'Video Count':<10}")
    print("=" * 60)
    for chapter, count in sorted(chapter_counts.items()):
        print(f"{chapter:<45} | {count:<10}")
    print("=" * 60)
    print(f"{'TOTAL VIDEOS':<45} | {total_count:<10}")
    print("=" * 60)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "output"
    count_videos(target)
