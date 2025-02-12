#!/bin/bash

# 티스토리 백업 HTML 파일들이 있는 최상위 디렉토리 (티스토리 백업 폴더)
BASE_DIR="junhkang-1-1"

# 변환된 Markdown 파일들을 저장할 Hugo 콘텐츠 폴더 (예: content/posts)
OUTPUT_DIR="content/posts"

# OUTPUT_DIR가 없으면 생성
mkdir -p "$OUTPUT_DIR"

# BASE_DIR 내의 각 하위 폴더(게시글 번호별)를 순회
for dir in "$BASE_DIR"/*/; do
    # 하위 폴더 이름(게시글 번호)을 추출 (예: "1", "2", ...)
    post_id=$(basename "$dir")

    # 해당 폴더 내의 HTML 파일을 찾기 (첫 번째 HTML 파일만 선택)
    html_file=$(find "$dir" -maxdepth 1 -type f -name "*.html" | head -n 1)

    if [ -n "$html_file" ]; then
        # "오블완" 태그 확인
        has_oblwan=$(grep -l "오블완" "$html_file")
        if [ -n "$has_oblwan" ]; then
            echo "⚠️ 오블완 태그 발견, 건너뜀: $html_file"
            continue
        fi

        # 제목 추출
        title=$(grep -Eo '<h2 class="title-article">.*?</h2>' "$html_file" | sed -E 's/.*<h2 class="title-article">([^<]+)<\/h2>.*/\1/' | sed 's/"/\\"/g')

        # 날짜 추출
        date=$(grep -Eo '<p class="date">.*?</p>' "$html_file" | sed -E 's/.*<p class="date">([^<]+)<\/p>.*/\1/')

        # 카테고리 추출
        categories=$(grep -Eo '<p class="category">.*?</p>' "$html_file" | sed 's/<[^>]*>//g')

        # Front Matter 추가
        safe_title=$(echo "$title" | sed "s/'/''/g" | tr -d '\000-\037')

        # 임시 파일들 생성
        temp_file=$(mktemp)
        temp_file2=$(mktemp)
        
        # HTML 파일의 이미지 경로를 수정하고 불필요한 요소 제거
        cat "$html_file" | sed -E '
            # 제목 및 메타 정보 제거
            /<h2 class="title-article">/d;
            /<p class="category">/d;
            /<p class="date">/d;
            # 링크 속성 제거 및 aria-hidden 제거
            s/<a([^>]*) aria-hidden="[^"]*"([^>]*)>/\1\2/g;
            s/<a href="([^"]+)"[^>]*>/\1/g;
            s/<\/a>//g;
            # ke-size 속성과 id 속성 제거
            s/ ke-size="[^"]*"//g;
            s/ id="[^"]*"//g;
            s/ aria-hidden="[^"]*"//g;
            # 헤더 태그에서 {#...} 형식의 모든 태그 제거
            s/([[:space:]]|^)\{#[^}]+ ke-size="[^"]+"\}//g;
            s/([[:space:]]|^)\{#[^}]+\}//g;
            # 이미지 경로 수정
            s|src="\.\/img\/([^"]+)"|src="/images/posts/'$post_id'/\1"|g;
            s|src="([^/][^"]+)"|src="/images/posts/'$post_id'/\1"|g
        ' > "$temp_file"

        # Markdown으로 변환
        pandoc -f html -t markdown+pipe_tables-raw_html --wrap=none "$temp_file" | grep -v '^[[:space:]]*:.*$' > "$temp_file2"

        # 해시태그 추출 (마지막 줄에서)
        tags=$(tail -n 1 "$temp_file2" | grep -o '#[^ ]\+' | sed 's/#//g' | tr '\n' ',' | sed 's/,$//')

        # Front Matter with tags
        if [ -n "$tags" ]; then
            front_matter="---
title: '$safe_title'
date: '$date'
categories: ['$categories']
tags: ['$(echo $tags | sed "s/,/', '/g")']
---"
        else
            front_matter="---
title: '$safe_title'
date: '$date'
categories: ['$categories']
---"
        fi

        # 변환된 Markdown 파일 경로
        output_file="$OUTPUT_DIR/$post_id.md"

        # Front Matter 저장
        echo -e "$front_matter" > "$output_file"

        # 마지막 해시태그 줄을 제외한 나머지 내용을 저장
        sed '$d' "$temp_file2" | sed -E '
            # 이스케이프된 마크다운 문법 수정
            s/\\\*\\\*/\*\*/g;
            s/\\\`/`/g;
            s/\\"/"/g;
            s/\\\[/[/g;
            s/\\\]/]/g;
            s/\\\\/\\/g;
            # 불필요한 백슬래시 제거
            s/\\$//g;
            # 헤더의 ID 태그와 ke-size 완전 제거
            s/([[:space:]]|^)\{#[^}]+ ke-size="[^"]+"\}//g;
            s/([[:space:]]|^)\{#[^}]+\}//g;
            s/[[:space:]]*\{#[^}]+\}//g;
            # lightbox 형식의 이미지 태그 처리
            s/\[ *!\[[^]]*\](\([^)]+\)) *\]\{lightbox="lightbox"\}/!\[\]\1/g;
            s/\[ *!\[\](\([^)]+\)) *\]\{lightbox="lightbox"\}/!\[\]\1/g;
            # 일반 이미지 처리
            s|!\[\]\(([^/][^)]+)\)|<img src="/images/posts/'$post_id'/\1" />|g;
            s|!\[\]\(/images/posts/[^)]+\)|<img src="\0" />|g;
            # 불필요한 따옴표 제거
            s/^"//g;
            s/"$//g;
            # 연속된 빈 줄 제거
            /^[[:space:]]*$/N;/^\n[[:space:]]*$/D
        ' >> "$output_file"

        # 임시 파일 삭제
        rm "$temp_file" "$temp_file2"

        echo "✅ 변환 완료: $output_file"
    else
        echo "No HTML file found in $dir"
    fi
done
