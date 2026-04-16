# baekjoon-crawl

백준 문제를 `problem/{id}` 경로로 직접 순회하면서, 각 문제를 개별 Markdown 파일로 저장하는 크롤러입니다.

이 프로젝트는 아래를 목표로 합니다.

- 문제 본문을 `md` 파일로 저장
- 문제 본문 안의 이미지 자산을 로컬에 저장
- 실패한 요청을 별도 로그로 남기고 나중에 재시도
- 프로세스가 중간에 꺼져도 이미 저장된 파일은 건너뛰고 이어서 진행

## 요구 사항

- `python3` 3.10 이상 권장
- macOS / Linux / WSL 환경 권장
- 외부 네트워크 접근 가능 환경

## 설치

프로젝트 루트에서 아래 순서로 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

설치가 끝났는지 확인하려면:

```bash
python3 -m py_compile crawl_baekjoon.py
```

## 빠른 시작

전체 범위를 수집하려면:

```bash
python3 crawl_baekjoon.py \
  --start-id 1000 \
  --end-id 34506 \
  --output-dir archive \
  --workers 2
```

몇 개만 테스트하려면:

```bash
python3 crawl_baekjoon.py \
  --only-ids 1000,1001,2557 \
  --output-dir archive-test
```

특정 구간만 수집하려면:

```bash
python3 crawl_baekjoon.py \
  --start-id 1000 \
  --end-id 1999 \
  --output-dir archive-partial \
  --workers 4
```

## 출력 구조

기본 출력은 아래처럼 생깁니다.

```text
archive/
  manifest.jsonl
  failed_requests.jsonl
  problems/
    1000.md
    1001.md
  assets/
    1000/
      image-1-<hash>.png
```

각 파일의 의미:

- `problems/{id}.md`
  - 실제 문제 본문 Markdown
- `assets/{problem_id}/...`
  - 문제 본문에 포함된 이미지 파일
- `manifest.jsonl`
  - 저장 완료된 문제 메타데이터
- `failed_requests.jsonl`
  - 끝내 실패한 문제/이미지 요청 로그

## 저장되는 내용

각 문제의 Markdown에는 다음만 저장합니다.

- 문제 제목
- 문제
- 입력
- 출력
- 힌트
- 예제 입력 / 예제 출력

아래 정보는 `manifest.jsonl`에 따로 저장합니다.

- 문제 번호
- 제목
- 원본 URL
- 저장 경로
- assets 디렉터리
- 태그
- 시간 제한 / 메모리 제한 / 제출 / 정답 수 / 정답 비율

## 재시작 복구

프로세스가 중간에 꺼져도 같은 명령을 다시 실행하면 이어서 진행할 수 있습니다.

복구 방식은 두 단계입니다.

- 이미 존재하는 `problems/{id}.md` 파일은 다시 받지 않음
- 이미 존재하는 이미지 파일도 다시 받지 않음

예시:

```bash
python3 crawl_baekjoon.py \
  --start-id 1000 \
  --end-id 34506 \
  --output-dir archive \
  --workers 2
```

위 명령이 중간에 멈췄다면 같은 명령을 그대로 다시 실행하면 됩니다.

## 실패 로그와 재시도

한 URL은 기본적으로 최대 `3번`까지 시도합니다.

3번 모두 실패하면 `failed_requests.jsonl`에 기록합니다.

실패 로그에는 아래 정보가 들어갑니다.

- `resource_type`
  - `problem` 또는 `image`
- `url`
- `problem_id`
- `output_path`
- `reason`
- `attempts`
- `status_code`
- `source_problem_id`
- `failed_at`

이미지 실패 로그는 나중에 재시도할 수 있도록, 어느 문제의 어떤 로컬 경로에 저장해야 하는지도 같이 기록합니다.

실패 로그만 다시 시도하려면:

```bash
python3 crawl_baekjoon.py \
  --output-dir archive \
  --retry-log-input archive/failed_requests.jsonl \
  --workers 2
```

주의:

- 재시도할 때는 보통 원래와 같은 `--output-dir`를 쓰는 편이 안전합니다.
- 이미 파일이 있으면 재시도 중에도 건너뜁니다.

## 주요 옵션

### 범위 지정

```bash
--start-id 1000
--end-id 34506
```

- 기본 시작 번호는 `1000`
- `--end-id`를 지정하지 않으면 전체 범위를 만들 수 없으므로 실행되지 않음

### 특정 문제만 실행

```bash
--only-ids 1000,1001,2557
```

- 쉼표로 구분한 문제 번호만 수집
- `start-id`, `end-id` 필터도 함께 적용됨

### 출력 경로

```bash
--output-dir archive
```

- 결과 파일이 저장될 루트 디렉터리

### 병렬 처리

```bash
--workers 2
```

- 문제 페이지 크롤링용 worker 스레드 수
- 사이트 부하를 고려하면 `2~4`부터 시작하는 것이 안전

### 요청 간격

```bash
--delay 0.7
```

- 전체 스레드가 공유하는 요청 간격
- `workers`를 올려도 요청이 한꺼번에 폭주하지 않게 제한

### 타임아웃

```bash
--timeout 60
```

- 개별 요청 타임아웃
- 사이트 응답이 느릴 때를 감안해 기본값은 `60초`

### 실패 로그 입력

```bash
--retry-log-input archive/failed_requests.jsonl
```

- 실패 로그 파일에 들어 있는 요청만 다시 시도

### 실패 로그 출력 위치

```bash
--failure-log-path logs/failed_requests.jsonl
```

- 실패 로그 저장 경로를 직접 지정
- 지정하지 않으면 `output-dir/failed_requests.jsonl`

## manifest 동작

`manifest.jsonl`은 append-only 형식이지만, 같은 문제 번호를 중복으로 다시 추가하지 않도록 처리합니다.

즉:

- 이미 manifest에 기록된 문제는 다시 append하지 않음
- 재시도 성공분도 기존 문제 번호가 있으면 append하지 않음

## 운영 팁

- 전체 수집은 오래 걸리므로 `tmux`나 `screen` 안에서 실행하는 편이 안전합니다.
- 너무 공격적으로 `workers`를 키우거나 `delay`를 낮추면 타임아웃과 차단 가능성이 올라갑니다.
- 먼저 작은 범위로 테스트한 뒤 전체 수집으로 넘어가는 편이 안전합니다.

권장 시작값:

```bash
python3 crawl_baekjoon.py \
  --start-id 1000 \
  --end-id 1999 \
  --output-dir archive-test \
  --workers 2 \
  --delay 0.7
```

## 주의 사항

- 이 스크립트는 백준의 정답 판정 시스템을 복제하지 않습니다.
- 저장 대상은 문제 본문과 관련 자산입니다.
- 사이트 정책, robots, 이용 약관, 저작권 이슈는 직접 확인하고 운영해야 합니다.
