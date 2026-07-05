import os
import re
import sys
import html

def sanitize_filename(text, max_len=180):
    name = text.strip()
    name = re.sub(r'[\s:/\\|]+', '_', name)
    name = re.sub(r'[^\w.\-]+', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    if len(name) > max_len:
        name = name[:max_len].rstrip('_')
    return name or "untitled"

def main(html_path="check.html", output_dir="output"):
    if not os.path.exists(html_path):
        print(f"Error: HTML file '{html_path}' not found.")
        sys.exit(1)
    if not os.path.exists(output_dir):
        print(f"Error: Output directory '{output_dir}' not found.")
        sys.exit(1)

    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Collapse consecutive spaces and newlines to a single space
    collapsed = re.sub(r'\s+', ' ', content)

    # Find matches of format: >Section X: Section Name</span>
    matches = re.findall(r'>Section\s+(\d+):\s*(.*?)</span>', collapsed)

    if not matches:
        print("No sections found in HTML file. Please check the structure.")
        sys.exit(1)

    print(f"Found {len(matches)} sections in '{html_path}'. Processing renames...")

    renamed_count = 0
    skipped_count = 0
    not_found_count = 0

    for sec_num_str, sec_name in matches:
        sec_num = int(sec_num_str)
        sec_name = html.unescape(sec_name.strip())
        
        prefix = f"{sec_num:02d}"
        sanitized_name = sanitize_filename(sec_name)
        new_name = f"{prefix}{sanitized_name}"
        
        old_path = os.path.join(output_dir, sanitized_name)
        new_path = os.path.join(output_dir, new_name)
        
        # Check if the old directory exists
        if os.path.isdir(old_path):
            try:
                os.rename(old_path, new_path)
                print(f"Renamed: '{sanitized_name}' -> '{new_name}'")
                renamed_count += 1
            except Exception as e:
                print(f"Error renaming '{sanitized_name}': {e}")
        elif os.path.isdir(new_path):
            print(f"Already renamed: '{new_name}'")
            skipped_count += 1
        else:
            print(f"Not found: '{sanitized_name}' (expected '{new_name}')")
            not_found_count += 1

    print("\n" + "="*50)
    print("Renaming Summary:")
    print("="*50)
    print(f"Successfully Renamed : {renamed_count}")
    print(f"Already Renamed      : {skipped_count}")
    print(f"Not Found/Missing    : {not_found_count}")
    print("="*50)

if __name__ == "__main__":
    main()
