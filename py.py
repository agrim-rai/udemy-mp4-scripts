import json
import csv
import os
import re

def sanitize_name(text):
    # spaces and common separators become underscores for folder/file names
    name = text.strip()
    name = re.sub(r'[\s:/\\|]+', '_', name)
    name = re.sub(r'[^\w.\-]+', '_', name)
    name = re.sub(r'_+', '_', name)
    return name.strip('_')

def generate_udemy_csv():
    json_files = ['barijson0.json', 'barijsonMID.json', 'barijson1.json']
    course_slug = "datastructurescncpp"
    base_url = f"https://www.udemy.com/course/{course_slug}/learn/lecture/"

    all_items = []
    for file_name in json_files:
        if not os.path.exists(file_name):
            print(f"Warning: {file_name} not found. Skipping.")
            continue
        with open(file_name, 'r', encoding='utf-8') as f:
            try:
                data = json.load(f)
                if 'results' in data:
                    all_items.extend(data['results'])
            except json.JSONDecodeError:
                print(f"Error reading {file_name}. Ensure it is valid JSON.")

    # dedupe by id (pagination pages can overlap)
    seen_ids = set()
    unique_items = []
    for item in all_items:
        item_id = item.get('id')
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        unique_items.append(item)

    # higher sort_order = earlier in the course
    unique_items.sort(key=lambda x: x.get('sort_order', 0), reverse=True)

    lectures = []
    current_chapter = 'Uncategorized'
    lecture_num = 0

    for item in unique_items:
        item_class = item.get('_class')
        if item_class == 'chapter':
            current_chapter = sanitize_name(item.get('title', 'Uncategorized'))
        elif item_class == 'lecture':
            lecture_id = item.get('id')
            title = item.get('title', 'Untitled')
            lecture_num += 1
            formatted_title = f"{lecture_num:02d}_{sanitize_name(title)}"
            url = f"{base_url}{lecture_id}#overview"
            lectures.append({
                'chapter': current_chapter,
                'url': url,
                'title': formatted_title
            })

    csv_filename = "udemy_course_links.csv"
    with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Chapter', 'URL', 'Title'])
        for lec in lectures:
            writer.writerow([lec['chapter'], lec['url'], lec['title']])

    print(f"Successfully processed {len(lectures)} lectures across {len(set(l['chapter'] for l in lectures))} chapters.")
    print(f"Data saved to {csv_filename}")

if __name__ == '__main__':
    generate_udemy_csv()
