# copilot-hwp-skill

[![CI](https://github.com/rkttu/copilot-hwp-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/rkttu/copilot-hwp-skill/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/rkttu/copilot-hwp-skill)](https://github.com/rkttu/copilot-hwp-skill/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Microsoft 365 Copilot과 Copilot Studio 에이전트가 **한글(HWP/HWPX) 문서를 읽을 수 있게 해 주는 스킬**입니다.

[Agent Skills](https://agentskills.io) 표준(`SKILL.md`)을 따르는 스킬 패키지 `hwp-reader`에 Python 스크립트 하나가 들어 있습니다. 이 스크립트는 **Python 표준 라이브러리만 사용**합니다. 그래서 네트워크 접속과 패키지 설치가 막힌 Copilot 코드 실행 샌드박스(Linux)에서도 별도 준비 없이 동작합니다. OLE 복합 문서 파서와 배포용 문서 복호화에 쓰는 AES-128까지 모두 스크립트 안에 직접 구현했습니다.

## 주요 기능

- **HWP 5.x(.hwp)**: 압축/비압축 문서와 **배포용 문서**(복호화 내장)를 읽습니다.
- **HWPX(.hwpx)**: 한글 2014 이후 버전이 만든 문서와 다른 도구가 만든 OWPML 문서를 읽습니다.
- **구조 보존**
  - 표: 셀 병합과 중첩 표는 HTML `rowspan`/`colspan`으로 정확하게 표현합니다.
  - 개요(제목) 수준과 자동 문단 번호(`1.`, `가.`, `①` …)를 재현합니다.
  - 각주와 미주, 머리말과 꼬리말을 따로 정리합니다.
  - 하이퍼링크, 수식(한글 수식 스크립트), 그림, 글상자도 처리합니다.
  - 메모(대상 텍스트 포함), 숨은 설명, 바탕쪽, 덧말, 글자 겹침, 양식 체크박스도 읽습니다.
  - 서식 문서의 빈 누름틀은 `[입력란: 안내문]`으로 표시해 채워야 할 칸을 알려 줍니다.
  - 변경 추적 문서는 변경이 반영된 최종본으로 읽습니다.
- **출력 형식**: Markdown(기본), 일반 텍스트, JSON(표는 셀 좌표와 병합 정보가 담긴 격자), `--info`(메타데이터와 통계)
- **에이전트 친화적 입력/출력**
  - 형식을 확장자가 아니라 파일 내용으로 판별합니다.
  - `.zip`에 묶인 문서와 디렉터리 안의 문서도 읽습니다.
  - 긴 문서는 `--offset`/`--max-chars`로 나눠 읽습니다.
- **안전성**
  - 문서 내용을 실행하지 않고, 네트워크에도 접속하지 않습니다.
  - XML의 DTD와 엔티티를 거부합니다.
  - 압축 해제 크기, OLE 섹터 체인 순환, 중첩 깊이에 상한을 둡니다.
- **읽을 수 없는 문서는 이유를 알려 줍니다**: 암호/DRM 문서, HWP 3.x(한글 97) 이하, HWPML

## 설치

[Releases](https://github.com/rkttu/copilot-hwp-skill/releases)에서 `hwp-reader.zip`을 내려받습니다. zip 루트에 `SKILL.md`, `scripts/`, `references/`, `LICENSE.txt`가 들어 있습니다.

### Copilot Studio

GitHub Copilot 하네스를 쓰는 에이전트에 추가합니다.

1. 에이전트의 **빌드(Build)** 탭 → **스킬(Skills)** → **스킬 추가(Add skill)** → **스킬 업로드(Upload a skill)**
2. `hwp-reader.zip`을 업로드합니다. zip을 풀지 말고 그대로 올립니다.

### Microsoft 365 Copilot: Agent Builder

1. 선언적 에이전트의 **구성(Configure)** 탭 → **스킬(Skills)** → **추가(Add)**
2. `hwp-reader.zip` 전체를 업로드합니다. `SKILL.md`만 따로 올리면 거부됩니다.

### Microsoft 365 Agents Toolkit

1. zip을 풀어 `appPackage/skills/hwp-reader/`에 둡니다. Agents Toolkit은 zip이 아니라 폴더를 받습니다.
2. 선언적 에이전트 매니페스트(v1.9 이상)에 다음을 추가합니다.

   ```json
   "agent_skills": [{ "folder": "./skills/hwp-reader" }]
   ```

3. 환경 변수 `TEAMSFX_AGENT_SKILLS=true`를 설정합니다.
4. 파일을 내려받을 수 있도록 `CodeInterpreter` 기능을 켜 두는 것을 권장합니다.

## 사용 예

스킬을 추가한 에이전트에게 다음처럼 요청하면 됩니다.

- "첨부한 한글 파일 요약해 줘"
- "이 hwpx 문서에서 표만 뽑아서 엑셀로 만들어 줘"
- "공고문에서 제출 기한이랑 제출 서류 찾아 줘"
- "이 hwp 파일을 영어로 번역해 줘"

### 첨부 파일이 올라가지 않을 때

Microsoft 365 Copilot과 Copilot Studio의 채팅 첨부는 Office, PDF, 텍스트 같은 일부 형식만 받습니다. 그래서 `.hwp`/`.hwpx` 파일이 거부될 수 있습니다. 이럴 때는 다음과 같이 우회합니다.

- 문서를 **zip으로 압축해서** 첨부합니다. 스킬은 zip 안의 `.hwp`/`.hwpx`를 자동으로 찾아 읽습니다. Windows에서 만든 zip의 한글 파일 이름(CP949)도 처리합니다.
- zip도 거부되면 확장자를 허용되는 것으로 바꿔서(예: `.txt`) 첨부합니다. 스킬은 확장자가 아니라 파일 내용으로 형식을 판별합니다.

## 직접 실행하기

스크립트는 단독으로도 쓸 수 있습니다. 필요한 것은 Python 3.8 이상뿐입니다.

```bash
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp                  # Markdown 출력
python3 hwp-reader/scripts/hwp_reader.py 문서.hwpx -f text         # 일반 텍스트
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp -f json -o out.json
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp --info           # 메타데이터와 통계
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp --max-chars 15000              # 나눠 읽기: 첫 페이지
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp --max-chars 15000 --offset N   # N = 앞 페이지 끝 안내문의 숫자
python3 hwp-reader/scripts/hwp_reader.py 묶음.zip -o 결과폴더/       # zip 안의 문서를 각각 변환
python3 hwp-reader/scripts/hwp_reader.py 문서.hwp --extract-images 그림폴더/
```

| 옵션 | 설명 |
| --- | --- |
| `-f markdown\|text\|json` | 출력 형식 (기본 `markdown`) |
| `-o PATH` | 파일로 저장 (UTF-8). 입력이 여러 문서로 펼쳐지거나(여러 파일, zip, 디렉터리) `PATH`가 이미 있는 디렉터리이거나 `/`로 끝나면 그 디렉터리에 문서마다 파일을 하나씩 만듦 |
| `--info` | 메타데이터, 통계, 미리보기, 경고를 JSON으로 출력 |
| `--tables auto\|markdown\|html\|text` | 표 표현 방식 (기본 `auto`: 단순 표는 Markdown, 병합/중첩 표는 HTML) |
| `--max-chars N`, `--offset N` | 출력의 일부만 보기(페이징). 다음 페이지의 `N`은 앞 페이지 끝 안내문에 나옴. JSON에는 쓸 수 없음 |
| `--extract-images DIR` | 본문 그림을 저장하고 출력에서 링크 (문서가 여럿이면 문서별 하위 폴더) |
| `--no-headers-footers`, `--no-notes`, `--no-comments` | 머리말/꼬리말과 바탕쪽, 각주/미주, 메모/숨은 설명 제외 |

종료 코드는 다음과 같습니다.

| 코드 | 의미 |
| --- | --- |
| 0 | 성공 |
| 1 | 파일 없음 또는 예기치 않은 오류 |
| 2 | 잘못된 인자 |
| 3 | 지원하지 않는 형식 |
| 4 | 암호/DRM 문서 |
| 5 | 손상된 파일 |

### Python에서 사용하기

```python
import sys
sys.path.insert(0, "hwp-reader/scripts")
import hwp_reader

doc = hwp_reader.read_document("문서.hwp")   # 경로, bytes, 바이너리 파일 객체
print(doc.metadata)                          # 제목, 작성자, 날짜 …
print(doc.to_markdown())
data = doc.to_dict()                         # JSON 구조
```

출력 규칙과 JSON 구조, 문제 해결 방법은 [`hwp-reader/references/REFERENCE.md`](hwp-reader/references/REFERENCE.md)를 참고하세요.

## 제한 사항

- 텍스트와 문서 구조만 추출합니다. 쪽 배치, 글꼴, 색, 쪽 번호는 재현하지 않습니다.
- 차트, OLE 개체, 동영상은 자리표시(`[차트]` 등)로만 나옵니다. 수식은 한글 수식 스크립트 그대로 나옵니다(LaTeX가 아님).
- 변경 추적 문서에서 여러 문단에 걸친 HWPX 삭제 구간은 출력에 남을 수 있습니다.
- 암호나 DRM이 걸린 문서, HWP 3.x(한글 97) 이하, HWPML(.hml)은 읽을 수 없습니다. 한글에서 `.hwpx`나 `.hwp`로 다시 저장하면 읽을 수 있습니다.

## 개발

```text
hwp-reader/                  ← 스킬 패키지 (zip으로 배포되는 부분)
  SKILL.md                   ← 에이전트용 지침 (frontmatter: name, description)
  scripts/hwp_reader.py      ← 단일 파일 리더 (표준 라이브러리만 사용)
  references/REFERENCE.md    ← 상세 레퍼런스
tests/
  test_hwp_reader.py         ← 단위 테스트 (합성 HWP/HWPX 생성기로 검증)
  test_corpus.py             ← 실제 문서 코퍼스 회귀 테스트 (코퍼스가 있을 때만 실행)
  builders.py                ← 테스트용 CFB/HWP/HWPX 작성기
  corpus-manifest.json       ← 코퍼스 출처, 라이선스, 해시
tools/
  build_skill.py             ← 스킬 검증과 zip 빌드
  fetch_corpus.py            ← 테스트 코퍼스 내려받기
```

```bash
python3 -m unittest discover -s tests -v       # 단위 테스트
python3 tools/fetch_corpus.py                  # 코퍼스 582개 내려받기 (.corpus/, git 제외)
python3 -m unittest tests.test_corpus -v       # 코퍼스 회귀 테스트
python3 tools/build_skill.py                   # 검증 후 dist/hwp-reader.zip 생성
```

검증 수준은 다음과 같습니다.

- 단위 테스트는 합성 HWP/HWPX 파일과 퍼징으로 파서 동작을 확인합니다.
- 코퍼스 테스트는 공개 저장소에서 모은 실제 문서 582개(HWP 291, HWPX 278, 기타 13)를 모두 처리합니다. 565개를 읽고, 나머지 17개(HWP 3.x 12개, 암호 문서 4개, 일부러 깨뜨린 파일 1개)는 의도대로 이유를 밝혀 거부합니다.
- 한글이 저장할 때 함께 기록하는 미리보기 텍스트(PrvText)를 독립 기준으로 삼아 비교합니다. 미리보기가 20자 이상인 문서 258개 모두에서 미리보기 글자의 95% 이상, 255개에서 98% 이상을 추출합니다. 남은 차이는 한글 전용 기호(PUA)를 표준 문자로 바꿔 쓴 부분과 변경 추적 표시입니다.
- 견고성 검증으로, 악의적으로 만든 입력(셀 병합·번호·압축 폭탄, 순환 섹터 체인, 깨진 XML, 수천 단계 중첩 등)이 메모리 2GB 제한 안에서 수 초 내에 끝나는지 확인했습니다.
- `tools/build_skill.py`는 Microsoft 업로드 규칙을 검사합니다: 허용 확장자, 디렉터리 깊이, 파일 수와 크기, `name`과 폴더 이름 일치, BOM 없는 UTF-8, 표준 라이브러리 외 import 금지.

CI는 Linux 컨테이너에서 Python 3.8~3.15를 테스트하고, Windows와 macOS에서도 테스트합니다. `vX.Y.Z` 형식의 태그(예: `v0.1.0`)를 푸시하면 릴리스 워크플로가 테스트를 거쳐 `hwp-reader.zip`을 GitHub Release에 올립니다.

### 릴리스 절차

1. `hwp-reader/scripts/hwp_reader.py`의 `__version__`을 올립니다. 태그 버전과 다르면 릴리스가 실패합니다.
2. `CHANGELOG.md`에 `## [X.Y.Z] - 날짜` 섹션을 추가합니다. 릴리스 노트로 쓰입니다.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`

## 라이선스

[Apache License 2.0](LICENSE)

Copyright 2026 rkttu@rkttu.com (남정현)

본 제품은 한글과컴퓨터의 한/글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다. "한글", "한컴", "HWP", "HWPX"는 (주)한글과컴퓨터의 상표입니다. 이 프로젝트는 (주)한글과컴퓨터나 Microsoft와 관련이 없습니다.
