# 변경 기록

이 프로젝트는 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다.

## [0.1.0] - 2026-11-09

첫 릴리스입니다.

### 추가

- `hwp-reader` 스킬: Microsoft 365 Copilot(Agent Builder, Agents Toolkit)과 Copilot Studio(GitHub Copilot 하네스)에서 쓰는 Agent Skills 패키지
- `scripts/hwp_reader.py`: Python 표준 라이브러리만 쓰는 HWP/HWPX 리더
  - HWP 5.x: OLE 복합 문서 파서 내장, 압축/비압축 문서, 배포용 문서(AES-128 복호화 내장)
  - HWPX(OWPML): 한글이 만든 문서와 다른 도구가 만든 문서
  - 표(병합, 중첩, 캡션), 개요와 자동 문단 번호, 글머리표, 각주/미주, 머리말/꼬리말, 바탕쪽, 하이퍼링크, 수식, 그림, 글상자, 메모(대상 텍스트 포함), 숨은 설명, 덧말, 글자 겹침, 양식 체크박스, 누름틀 안내문
  - 변경 추적 문서는 최종본으로 읽기
  - Markdown, 텍스트, JSON 출력과 `--info` 요약
  - zip 묶음과 디렉터리 입력, 내용 기반 형식 판별, `--offset`/`--max-chars` 페이징, 그림 추출
  - 손상된 HWPX XML에서 텍스트 복구, 본문을 읽지 못할 때 미리보기 텍스트로 대체
  - 암호/DRM, HWP 3.x, HWPML 문서는 이유가 담긴 오류와 종료 코드로 알림
- 견고성: 문서별 압축 해제·레코드·XML 요소 예산(한도에 닿으면 그때까지 읽은 내용을 경고와 함께 출력), 셀 병합·표 크기·번호 길이 제한, 순환/증폭 OLE 섹터 체인 차단, UTF-8 이외 XML과 DTD 거부, 깨진 XML 선형 복구
- 단위·회귀 테스트(82개), 실제 문서 582개 코퍼스 회귀 테스트, 스킬 패키지 검증/빌드 도구
- GitHub Actions: Linux(Python 3.8~3.15), Windows, macOS 테스트와 태그 기반 릴리스
