# 실거래 모듈 설계 계획서 (100점 품질)

## 0. 현재 상태 진단

### 있는 것 (재사용)
| 인프라 | 파일:라인 | 상태 |
|--------|-----------|------|
| TESTNET/LIVE 엔드포인트 전환 | `core_constants.py:44-57` | 완료 |
| API 키 로딩 (환경변수 우선) | `core_intel.py:243-248` | 완료 |
| 심볼 필터 (stepSize, tickSize) | `core_intel.py:379-381` | 완료 |
| 수량 포맷팅 (_format_qty) | `core_trader.py:259-261` | 완료 |
| 스코어링/청산 판단 로직 전체 | `core_logic.py` 513줄 | 변경 없음 |
| PnL 계산 공식 | `core_trader.py:360-363` | 유지 (교차검증용) |
| DB 영속화 (SQLite WAL) | `core_trader.py:53-78` | 유지 |
| IronShield 보호 체계 | `core_trader.py:140-155` | 유지 |
| 텔레그램 알림 | `core_trader.py:371-427` | 유지 |
| 테스트 인프라 (1,075줄) | `tests/` | 확장 |

### 없는 것 (신규 구현)
| 항목 | 중요도 | 신규 파일 |
|------|--------|-----------|
| HMAC-SHA256 서명 | P0 | `binance_signer.py` |
| 주문 실행 (MARKET) | P0 | `order_executor.py` |
| 레버리지/마진 설정 | P0 | `order_executor.py` |
| 체결 확인 + 실체결가 반영 | P0 | `order_executor.py` |
| 실잔고 동기화 | P0 | `order_executor.py` |
| 포지션 모드 검증 (One-Way) | P0 | `order_executor.py` |
| User Data Stream (WS fills) | P1 | `order_executor.py` |
| 포지션 리콘실리에이션 | P1 | `order_executor.py` |
| 레이트 리미터 | P1 | `order_executor.py` |
| 킬 스위치 | P0 | `order_executor.py` |
| 주문 감사 로그 | P1 | `order_executor.py` |

---

## 1. 아키텍처: Executor 패턴 (Strategy Pattern)

### 1-1. 핵심 원칙
- **core_trader.py의 판단 로직은 한 줄도 안 건드린다**
- 주문 "실행"만 Executor로 위임
- PaperExecutor (현재 동작) / BinanceExecutor (실거래) 런타임 전환
- 모든 변경은 하위 호환 (기존 페이퍼 모드 동일 동작 보장)

### 1-2. 의존 관계
```
shark_sovereign_v43.py (오케스트레이터)
    │
    ├── core_logic.py          ← 변경 없음
    ├── core_intel.py          ← 변경 없음
    ├── core_constants.py      ← 상수 3개 추가
    │
    └── core_trader.py         ← Executor 주입 (최소 변경)
            │
            └── order_executor.py  ← 신규 (전체 실행 로직)
                    │
                    └── binance_signer.py  ← 신규 (HMAC 유틸)
```

### 1-3. 데이터 흐름 (실거래 모드)
```
스코어링 → open_position() → executor.open_order()
                                    │
                                    ├─ 레버리지 설정 (POST /fapi/v1/leverage)
                                    ├─ 마켓 주문 (POST /fapi/v1/order)
                                    ├─ 체결 확인 (응답의 avgPrice, executedQty)
                                    └─ OrderResult 반환
                                            │
                              core_trader가 실체결가로 포지션 생성
                                            │
청산 판단 → close_position() → executor.close_order()
                                    │
                                    ├─ 반대매매 주문 (POST /fapi/v1/order)
                                    ├─ 체결 확인
                                    └─ OrderResult 반환
                                            │
                              core_trader가 실체결가로 PnL 확정
```

---

## 2. 신규 파일 설계

### 2-1. `binance_signer.py` (HMAC-SHA256 서명 유틸)

```python
"""Binance Futures HMAC-SHA256 request signing."""
import hmac
import hashlib
import time
from urllib.parse import urlencode

class BinanceSigner:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret.encode('utf-8')

    def sign_params(self, params: dict) -> dict:
        """Add timestamp + HMAC signature to params dict."""
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 5000
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret,
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        return params

    @property
    def headers(self) -> dict:
        return {'X-MBX-APIKEY': self.api_key}
```

**설계 포인트**:
- 순수 함수 — 상태 없음, 테스트 용이
- `api_secret`는 메모리에 bytes로만 보관 (문자열 GC 방지)
- `recvWindow=5000ms` (바이낸스 권장)
- 서명은 query_string 기반 (바이낸스 Futures 표준)

---

### 2-2. `order_executor.py` (핵심 모듈)

#### A. 데이터 클래스

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[int] = None
    avg_price: float = 0.0        # 실제 체결가 (핵심!)
    executed_qty: float = 0.0     # 실제 체결 수량
    commission: float = 0.0       # 실제 수수료
    status: str = ""              # FILLED, PARTIALLY_FILLED, REJECTED, etc.
    error_code: Optional[int] = None
    error_msg: str = ""
    raw_response: Optional[dict] = None  # 감사 로그용 원본
```

#### B. OrderExecutor ABC (프로토콜)

```python
from abc import ABC, abstractmethod

class OrderExecutor(ABC):
    @abstractmethod
    async def open_order(self, symbol: str, side: str, qty: float,
                         leverage: int) -> OrderResult:
        """포지션 오픈 주문. side='LONG'|'SHORT'"""

    @abstractmethod
    async def close_order(self, symbol: str, side: str,
                          qty: float) -> OrderResult:
        """포지션 종료 반대매매."""

    @abstractmethod
    async def get_balance(self) -> float:
        """USDT 가용 잔고 조회."""

    @abstractmethod
    async def get_positions(self) -> dict:
        """거래소 실 포지션 목록 조회."""

    @abstractmethod
    async def emergency_close_all(self) -> list:
        """킬 스위치: 모든 포지션 즉시 청산."""

    @abstractmethod
    async def initialize(self):
        """시작 시 검증 (포지션 모드, 마진 타입 등)."""
```

#### C. PaperExecutor (현재 동작 래핑)

```python
class PaperExecutor(OrderExecutor):
    """현재 페이퍼 트레이딩 동작을 그대로 보존."""

    def __init__(self, wallet: dict):
        self.wallet = wallet

    async def open_order(self, symbol, side, qty, leverage) -> OrderResult:
        # 현재 동작: 요청가 = 체결가 가정 (슬리피지 없음)
        return OrderResult(success=True, avg_price=0.0,  # caller가 price 전달
                          executed_qty=qty, commission=0.0,
                          status="FILLED")

    async def close_order(self, symbol, side, qty) -> OrderResult:
        return OrderResult(success=True, avg_price=0.0,
                          executed_qty=qty, commission=0.0,
                          status="FILLED")

    async def get_balance(self) -> float:
        return self.wallet.get('balance', 0.0)

    async def get_positions(self) -> dict:
        return {}  # 페이퍼 모드는 로컬 상태만 사용

    async def emergency_close_all(self) -> list:
        return []

    async def initialize(self):
        pass  # 검증 불필요
```

**설계 포인트**: PaperExecutor는 현재 core_trader.py의 시뮬레이션 동작과 100% 동일한 결과를 반환. 기존 테스트 전부 통과 보장.

#### D. BinanceExecutor (실거래 핵심)

```python
class BinanceExecutor(OrderExecutor):
    """Binance Futures USDT-M 실거래 주문 실행기."""

    def __init__(self, session: aiohttp.ClientSession, config: dict,
                 signer: BinanceSigner):
        self.session = session
        self.signer = signer
        self.base_url = FAPI_REST_BASE
        self._leverage_cache: dict[str, int] = {}  # 심볼별 레버리지 설정 캐시
        self._rate_limiter = RateLimiter()
        self._order_log: list = []  # 감사 로그
        self.max_capital = config.get('system', {}).get('max_capital', 1000.0)
```

##### D-1. 초기화 (시작 시 1회)

```python
async def initialize(self):
    """부팅 시 거래소 상태 검증 + 포지션 동기화."""
    # 1. 포지션 모드 확인 (One-Way 강제)
    mode = await self._get_position_mode()
    if mode['dualSidePosition']:
        raise RuntimeError(
            "Hedge Mode 활성화 상태. "
            "바이낸스에서 One-Way Mode로 변경 후 재시작하세요."
        )

    # 2. 계좌 잔고 확인
    balance = await self.get_balance()
    logging.info(f"💰 Exchange Balance: ${balance:,.2f}")
    if balance < 10.0:
        raise RuntimeError(f"잔고 부족: ${balance:.2f}")

    # 3. 기존 포지션 확인 (재시작 시 복구용)
    positions = await self.get_positions()
    if positions:
        logging.warning(
            f"⚠️ {len(positions)}개 기존 포지션 감지: "
            f"{list(positions.keys())}"
        )

    # 4. 연결 확인
    await self._ping()
    logging.info("✅ BinanceExecutor 초기화 완료")
```

##### D-2. 주문 실행 (`open_order`)

```python
async def open_order(self, symbol, side, qty, leverage) -> OrderResult:
    """
    포지션 오픈 전체 파이프라인:
    1. 잔고 사전 검증 (max_capital)
    2. 레버리지 설정
    3. 마켓 주문 전송
    4. 체결 확인
    """
    # --- SAFETY GATE 1: 자본 상한선 ---
    balance = await self.get_balance()
    if balance > self.max_capital:
        logging.critical(
            f"🚨 자본 상한선 초과: ${balance:.2f} > "
            f"${self.max_capital:.2f}. 주문 거부."
        )
        return OrderResult(success=False, error_msg="MAX_CAPITAL_EXCEEDED")

    # --- STEP 1: 레버리지 설정 (캐시 히트 시 스킵) ---
    if self._leverage_cache.get(symbol) != leverage:
        lev_ok = await self._set_leverage(symbol, leverage)
        if not lev_ok:
            return OrderResult(success=False, error_msg="LEVERAGE_SET_FAILED")
        self._leverage_cache[symbol] = leverage

    # --- STEP 2: 마켓 주문 ---
    order_side = 'BUY' if side == 'LONG' else 'SELL'
    params = {
        'symbol': symbol,
        'side': order_side,
        'type': 'MARKET',
        'quantity': str(qty),
        'newOrderRespType': 'RESULT',  # 즉시 체결 정보 포함
    }

    result = await self._signed_request('POST', '/fapi/v1/order', params)
    if not result:
        return OrderResult(success=False, error_msg="API_REQUEST_FAILED")

    # --- STEP 3: 응답 파싱 ---
    status = result.get('status', '')
    if status == 'FILLED':
        return OrderResult(
            success=True,
            order_id=result['orderId'],
            avg_price=float(result['avgPrice']),
            executed_qty=float(result['executedQty']),
            commission=self._extract_commission(result),
            status='FILLED',
            raw_response=result,
        )
    elif status == 'PARTIALLY_FILLED':
        # 부분 체결: 나머지 취소 후 체결분만 반환
        logging.warning(
            f"⚠️ 부분체결: {symbol} "
            f"{result['executedQty']}/{qty}"
        )
        await self._cancel_order(symbol, result['orderId'])
        filled_qty = float(result['executedQty'])
        if filled_qty > 0:
            return OrderResult(
                success=True,
                order_id=result['orderId'],
                avg_price=float(result['avgPrice']),
                executed_qty=filled_qty,
                commission=self._extract_commission(result),
                status='PARTIALLY_FILLED',
                raw_response=result,
            )
        return OrderResult(success=False, status='NO_FILL',
                          error_msg="부분체결 후 체결 수량 0")
    else:
        # REJECTED, EXPIRED, etc.
        return OrderResult(
            success=False, status=status,
            error_code=result.get('code'),
            error_msg=result.get('msg', 'Unknown'),
            raw_response=result,
        )
```

##### D-3. 주문 종료 (`close_order`)

```python
async def close_order(self, symbol, side, qty) -> OrderResult:
    """반대매매로 포지션 종료."""
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    params = {
        'symbol': symbol,
        'side': close_side,
        'type': 'MARKET',
        'quantity': str(qty),
        'reduceOnly': 'true',  # 안전장치: 반대 방향 신규 오픈 방지
        'newOrderRespType': 'RESULT',
    }

    result = await self._signed_request('POST', '/fapi/v1/order', params)
    if not result:
        return OrderResult(success=False, error_msg="CLOSE_API_FAILED")

    status = result.get('status', '')
    if status in ('FILLED', 'PARTIALLY_FILLED'):
        return OrderResult(
            success=True,
            order_id=result['orderId'],
            avg_price=float(result['avgPrice']),
            executed_qty=float(result['executedQty']),
            commission=self._extract_commission(result),
            status=status,
            raw_response=result,
        )

    return OrderResult(
        success=False, status=status,
        error_msg=result.get('msg', 'Close failed'),
        raw_response=result,
    )
```

##### D-4. 서명 요청 + 재시도 + 레이트리밋

```python
async def _signed_request(self, method: str, path: str,
                          params: dict, max_retries: int = 3
                          ) -> Optional[dict]:
    """
    HMAC 서명 + 재시도 + 레이트리밋 통합 요청.
    - 429 → 지수 백오프
    - 네트워크 에러 → 재시도
    - -1015 (Too many orders) → 1초 대기 후 재시도
    - -2019 (Margin insufficient) → 즉시 실패 (재시도 무의미)
    """
    url = f"{self.base_url}{path}"

    for attempt in range(max_retries):
        await self._rate_limiter.acquire()

        signed = self.signer.sign_params(params.copy())
        headers = self.signer.headers

        try:
            if method == 'POST':
                async with self.session.post(
                    url, data=signed, headers=headers, timeout=10
                ) as resp:
                    body = await resp.json()
                    self._log_order(method, path, signed, body, resp.status)

                    if resp.status == 200:
                        return body
                    elif resp.status == 429:
                        wait = min(30, 2 ** (attempt + 1))
                        logging.warning(f"429 Rate Limit. 대기 {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    else:
                        code = body.get('code', 0)
                        # 재시도 무의미한 에러
                        if code in (-2019, -1111, -1116, -4131):
                            logging.error(
                                f"❌ 치명적 주문 에러: {code} {body.get('msg')}"
                            )
                            return body
                        # 재시도 가능한 에러
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return body

            elif method == 'GET':
                async with self.session.get(
                    url, params=signed, headers=headers, timeout=10
                ) as resp:
                    body = await resp.json()
                    if resp.status == 200:
                        return body
                    elif resp.status == 429:
                        await asyncio.sleep(min(30, 2 ** (attempt + 1)))
                        continue

        except asyncio.TimeoutError:
            logging.warning(
                f"⏱ 타임아웃 {path} (시도 {attempt+1}/{max_retries})"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logging.error(f"❌ 요청 실패 {path}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    return None
```

##### D-5. 보조 메서드

```python
async def _set_leverage(self, symbol: str, leverage: int) -> bool:
    """POST /fapi/v1/leverage — 심볼별 레버리지 설정."""
    result = await self._signed_request('POST', '/fapi/v1/leverage', {
        'symbol': symbol,
        'leverage': leverage,
    })
    if result and 'leverage' in result:
        logging.info(f"⚙️ 레버리지 설정: {symbol} → {result['leverage']}x")
        return True
    logging.error(f"❌ 레버리지 설정 실패: {symbol} → {leverage}x")
    return False

async def _set_margin_type(self, symbol: str,
                            margin_type: str = 'CROSSED') -> bool:
    """POST /fapi/v1/marginType — CROSSED or ISOLATED."""
    result = await self._signed_request('POST', '/fapi/v1/marginType', {
        'symbol': symbol,
        'marginType': margin_type,
    })
    # -4046 = "No need to change margin type" (이미 설정됨)
    if result and (result.get('code') == 200 or result.get('code') == -4046):
        return True
    return False

async def get_balance(self) -> float:
    """GET /fapi/v2/account → USDT availableBalance."""
    result = await self._signed_request('GET', '/fapi/v2/account', {})
    if result:
        for asset in result.get('assets', []):
            if asset['asset'] == 'USDT':
                return float(asset['availableBalance'])
    return 0.0

async def get_positions(self) -> dict:
    """GET /fapi/v2/positionRisk → 실 포지션 목록."""
    result = await self._signed_request('GET', '/fapi/v2/positionRisk', {})
    positions = {}
    if result:
        for p in result:
            amt = float(p['positionAmt'])
            if abs(amt) > 0:
                positions[p['symbol']] = {
                    'type': 'LONG' if amt > 0 else 'SHORT',
                    'qty': abs(amt),
                    'entry': float(p['entryPrice']),
                    'unrealized_pnl': float(p['unRealizedProfit']),
                    'leverage': int(p['leverage']),
                    'margin_type': p['marginType'],
                }
    return positions

async def _get_position_mode(self) -> dict:
    """GET /fapi/v1/positionSide/dual."""
    return await self._signed_request(
        'GET', '/fapi/v1/positionSide/dual', {}
    ) or {}

async def _cancel_order(self, symbol: str, order_id: int):
    """DELETE /fapi/v1/order."""
    return await self._signed_request('DELETE', '/fapi/v1/order', {
        'symbol': symbol,
        'orderId': order_id,
    })

async def _ping(self):
    """GET /fapi/v1/ping — 연결 확인."""
    async with self.session.get(
        f"{self.base_url}/fapi/v1/ping", timeout=5
    ) as resp:
        if resp.status != 200:
            raise ConnectionError("Binance API 연결 실패")

async def emergency_close_all(self) -> list:
    """킬 스위치: 모든 포지션 즉시 마켓 청산."""
    positions = await self.get_positions()
    results = []
    for sym, pos in positions.items():
        r = await self.close_order(sym, pos['type'], pos['qty'])
        results.append((sym, r))
        logging.critical(f"🚨 EMERGENCY CLOSE: {sym} → {r.status}")
    return results
```

##### D-6. 레이트 리미터

```python
class RateLimiter:
    """
    Binance Futures 레이트 리밋 준수.
    - 요청 가중치: 1200/분
    - 주문 빈도: 10회/초, 1200회/분
    """
    def __init__(self, max_weight_per_min: int = 1000,
                 max_orders_per_sec: int = 8):
        self._weight_window: deque = deque()  # (timestamp, weight)
        self._order_window: deque = deque()   # timestamp
        self.max_weight = max_weight_per_min
        self.max_orders = max_orders_per_sec
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1):
        """레이트 리밋 초과 시 자동 대기."""
        async with self._lock:
            now = time.time()

            # 1분 윈도우 정리
            while self._weight_window and \
                  now - self._weight_window[0][0] > 60:
                self._weight_window.popleft()

            # 1초 윈도우 정리
            while self._order_window and \
                  now - self._order_window[0] > 1:
                self._order_window.popleft()

            # 가중치 초과 → 대기
            total_weight = sum(w for _, w in self._weight_window)
            if total_weight + weight > self.max_weight:
                wait = 60 - (now - self._weight_window[0][0])
                logging.warning(f"⏳ 레이트리밋 대기: {wait:.1f}s")
                await asyncio.sleep(wait + 0.1)

            # 주문 빈도 초과 → 대기
            if len(self._order_window) >= self.max_orders:
                wait = 1.0 - (now - self._order_window[0])
                if wait > 0:
                    await asyncio.sleep(wait + 0.05)

            self._weight_window.append((time.time(), weight))
            self._order_window.append(time.time())
```

##### D-7. 감사 로그

```python
def _log_order(self, method, path, params, response, status_code):
    """모든 API 요청/응답 기록. 시크릿 제외."""
    safe_params = {k: v for k, v in params.items()
                   if k != 'signature'}
    entry = {
        'ts': datetime.utcnow().isoformat(),
        'method': method,
        'path': path,
        'params': safe_params,
        'status': status_code,
        'response': response,
    }
    self._order_log.append(entry)
    # 최근 1000건만 유지
    if len(self._order_log) > 1000:
        self._order_log = self._order_log[-500:]

    # 주문 관련만 파일 로그
    if '/order' in path:
        logging.info(
            f"📋 [ORDER] {method} {path} "
            f"status={status_code} "
            f"resp={json.dumps(response, default=str)[:200]}"
        )
```

---

## 3. core_trader.py 변경 사항 (최소 침습)

### 3-1. `__init__` 변경

```python
# 변경 전 (현재)
class SharkTrader:
    def __init__(self, config: dict):
        ...
        self.wallet = {"balance": 10000.0}

# 변경 후
class SharkTrader:
    def __init__(self, config: dict, executor: OrderExecutor = None):
        ...
        self.executor = executor or PaperExecutor(self.wallet)
        self.wallet = {"balance": 10000.0}
```

### 3-2. `open_position` 변경

```python
# 현재 (line 284-285):
pos = {'entry': price, 'type': side, 'qty': qty, ...}
self.positions[symbol] = pos

# 변경 후:
result = await self.executor.open_order(symbol, side, qty, DEFAULT_LEVERAGE)
if not result.success:
    logging.warning(f"❌ 주문 실패: {symbol} {result.error_msg}")
    return False

# 실체결가 사용 (핵심 변경!)
fill_price = result.avg_price if result.avg_price > 0 else price
fill_qty = result.executed_qty if result.executed_qty > 0 else qty

pos = {
    'entry': fill_price,       # 실체결가
    'type': side,
    'qty': fill_qty,           # 실체결 수량
    'entry_margin': margin,
    'lev': DEFAULT_LEVERAGE,
    'start_time': time.time(),
    'details': {**details, "max_price": fill_price},
    'order_id': result.order_id,       # 신규 필드
    'commission': result.commission,   # 신규 필드
}
self.positions[symbol] = pos
```

### 3-3. `close_position` 변경

```python
# 현재 (line 308):
net_pnl = self._calculate_net_pnl(pos, exit_price)

# 변경 후:
result = await self.executor.close_order(symbol, pos['type'], pos['qty'])
if not result.success:
    logging.error(f"❌ 청산 주문 실패: {symbol} {result.error_msg}")
    pos['is_closing'] = False
    return

# 실체결가 기반 PnL
actual_exit = result.avg_price if result.avg_price > 0 else exit_price
net_pnl = self._calculate_net_pnl(pos, actual_exit)
```

### 3-4. 잔고 동기화 (주기적)

```python
async def sync_balance(self):
    """거래소 실잔고와 로컬 잔고 교차검증."""
    if isinstance(self.executor, PaperExecutor):
        return
    exchange_balance = await self.executor.get_balance()
    local_balance = self.wallet['balance']
    drift = abs(exchange_balance - local_balance)
    if drift > local_balance * 0.01:  # 1% 이상 불일치
        logging.warning(
            f"⚠️ 잔고 불일치: 거래소=${exchange_balance:.2f} "
            f"로컬=${local_balance:.2f} (차이: ${drift:.2f})"
        )
        # 거래소가 진실 → 로컬 보정
        self.wallet['balance'] = exchange_balance
```

### 3-5. 포지션 리콘실리에이션 (재시작 시)

```python
async def reconcile_positions(self):
    """로컬 DB 포지션과 거래소 실 포지션 동기화."""
    if isinstance(self.executor, PaperExecutor):
        return

    exchange_pos = await self.executor.get_positions()
    local_pos = set(self.positions.keys())
    exchange_syms = set(exchange_pos.keys())

    # Case 1: 거래소에 있는데 로컬에 없음 (수동 매매 or 재시작)
    for sym in exchange_syms - local_pos:
        logging.warning(f"⚠️ 미추적 포지션 발견: {sym}. 로컬에 등록.")
        ep = exchange_pos[sym]
        self.positions[sym] = {
            'entry': ep['entry'],
            'type': ep['type'],
            'qty': ep['qty'],
            'entry_margin': ep['entry'] * ep['qty'] / ep['leverage'],
            'lev': ep['leverage'],
            'start_time': time.time(),
            'details': {'mode': 'RECOVERED', 'max_price': ep['entry']},
        }

    # Case 2: 로컬에 있는데 거래소에 없음 (수동 청산 or 청산됨)
    for sym in local_pos - exchange_syms:
        logging.warning(f"⚠️ 유령 포지션 제거: {sym}")
        del self.positions[sym]
        await self._execute_db_task(self._db_remove_pos, sym)
```

---

## 4. core_constants.py 변경 사항

```python
# --- 신규 상수 (3개) ---
# 실거래 활성화 (명시적 opt-in)
LIVE_TRADING = bool(_s.get('live_trading', False))

# API 시크릿 (환경변수만 허용 — 설정 파일 금지)
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

# 최대 자본 상한선 (안전장치)
MAX_CAPITAL = float(_s.get('max_capital', 1000.0))
```

---

## 5. shark_sovereign_v43.py 변경 사항

### 5-1. Executor 생성 + 주입

```python
# start() 메서드 내부
async def start(self):
    self.session = aiohttp.ClientSession(...)

    # Executor 선택 (런타임)
    if LIVE_TRADING:
        if not BINANCE_API_SECRET:
            logging.critical("❌ BINANCE_API_SECRET 환경변수 미설정")
            return
        api_key = os.environ.get('BINANCE_API_KEY') or \
                  CONFIG.get('api', {}).get('binance_key')
        signer = BinanceSigner(api_key, BINANCE_API_SECRET)
        executor = BinanceExecutor(self.session, CONFIG, signer)
        await executor.initialize()
        mode_str = "TESTNET" if TESTNET else "🔴 LIVE TRADING"
    else:
        executor = PaperExecutor({"balance": 10000.0})
        mode_str = "PAPER (Simulation)"

    self.trader = core_trader.SharkTrader(CONFIG, executor=executor)
    self.trader.set_session(self.session)

    logging.info(f"🦈 Shark-Pulse {SYSTEM_VERSION} [{mode_str}]")
    logging.info(f"🌐 API: {FAPI_REST_BASE}")
```

### 5-2. 주기적 동기화 태스크

```python
async def run_sync_task(self):
    """30초마다 잔고/포지션 동기화."""
    while self.running:
        try:
            await self.trader.sync_balance()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Sync Error: {e}")
            await asyncio.sleep(10)
```

---

## 6. shark_config.json 변경

```json
{
  "system": {
    "testnet": true,
    "live_trading": false,
    "max_capital": 1000.0,
    "DEFAULT_LEVERAGE": 10,
    "max_positions": 5
  }
}
```

**활성화 매트릭스:**

| testnet | live_trading | 동작 |
|---------|-------------|------|
| any | false | PaperExecutor (현재 동작) |
| true | true | BinanceExecutor → testnet.binancefuture.com |
| false | true | BinanceExecutor → fapi.binance.com (실거래) |

---

## 7. 안전장치 체계 (5중 방어)

### Layer 1: 설정 레벨
- `live_trading: false` 기본값 (명시적 opt-in)
- `max_capital` 상한선 (초과 시 주문 거부)
- `BINANCE_API_SECRET` 환경변수만 허용 (설정 파일 금지)

### Layer 2: 초기화 검증
- 포지션 모드 확인 (One-Way 아니면 부팅 거부)
- 잔고 최소값 확인 ($10 미만이면 부팅 거부)
- API 연결 ping 확인

### Layer 3: 주문 레벨
- `reduceOnly: true` (종료 주문이 신규 포지션 오픈 방지)
- 레이트 리미터 (1000 weight/분, 8 orders/초 — 바이낸스 한도의 83%)
- 체결 확인 후에만 로컬 상태 변경

### Layer 4: 런타임 레벨
- 30초마다 잔고/포지션 리콘실리에이션
- IronShield 보호 체계 유지 (기존)
- Global PnL Stop 유지 (기존 -3%)

### Layer 5: 긴급 레벨
- `emergency_close_all()` 킬 스위치
- 텔레그램 CRITICAL 알림
- 감사 로그 (최근 1000건 API 기록)

---

## 8. 에러 코드 처리 매핑

| Binance 코드 | 의미 | 처리 |
|-------------|------|------|
| -2019 | Margin insufficient | 즉시 실패 + 텔레그램 알림 |
| -1111 | Invalid precision | 즉시 실패 (qty 포맷 버그) |
| -1116 | Invalid orderType | 즉시 실패 (코드 버그) |
| -4131 | reduceOnly 불가 | 즉시 실패 (포지션 이미 없음) |
| -1015 | Too many orders | 1초 대기 후 재시도 |
| -1021 | Timestamp mismatch | recvWindow 증가 후 재시도 |
| 429 | Rate limit | 지수 백오프 (2s, 4s, 8s) |
| 5xx | 서버 에러 | 지수 백오프 재시도 |

---

## 9. 테스트 계획

### 9-1. 단위 테스트 (신규)

```
tests/test_binance_signer.py
  - test_sign_params_adds_timestamp
  - test_sign_params_hmac_sha256_correct
  - test_headers_contains_api_key
  - test_sign_params_idempotent (원본 params 변경 안 함)

tests/test_order_executor.py
  - test_paper_executor_open_always_succeeds
  - test_paper_executor_close_always_succeeds
  - test_paper_executor_get_balance
  - test_binance_executor_open_fills → OrderResult 검증
  - test_binance_executor_open_rejected → 에러 처리
  - test_binance_executor_partial_fill → 취소 + 체결분 반환
  - test_binance_executor_leverage_cache_hit
  - test_binance_executor_rate_limit_429_retry
  - test_binance_executor_margin_insufficient_no_retry
  - test_binance_executor_emergency_close_all
  - test_binance_executor_initialize_hedge_mode_rejected
  - test_rate_limiter_weight_limit
  - test_rate_limiter_order_frequency

tests/test_core_trader.py (확장)
  - test_open_position_with_executor_fill_price
  - test_close_position_with_executor_fill_price
  - test_open_position_executor_failure_returns_false
  - test_close_position_executor_failure_recovers
  - test_sync_balance_corrects_drift
  - test_reconcile_orphan_positions
  - test_reconcile_ghost_positions
```

### 9-2. 통합 테스트

```
tests/test_integration_live.py
  - Mock aiohttp.ClientSession으로 전체 파이프라인
  - 스코어링 → open → update_pnl → exit → close 사이클
  - 재시작 시 포지션 복구 시나리오
  - 킬 스위치 시나리오
```

### 9-3. 테스트넷 E2E (수동)

```
1. testnet=true, live_trading=true 설정
2. 테스트넷 API 키 환경변수 설정
3. 부팅 → 초기화 검증 로그 확인
4. 스코어링 → 실제 테스트넷 주문 체결 확인
5. 체결가 ≠ 요청가 차이 (슬리피지) 로그 확인
6. PnL 업데이트 → 청산 → 잔고 반영 확인
7. emergency_close_all 테스트
8. 잔고 리콘실리에이션 동작 확인
```

---

## 10. 구현 순서

```
Step 1: binance_signer.py 생성 + 테스트          (30분)
Step 2: order_executor.py 생성                    (2시간)
  - OrderResult, OrderExecutor ABC
  - PaperExecutor (현재 동작 래핑)
  - BinanceExecutor 전체
  - RateLimiter
Step 3: core_trader.py 수정 (최소 침습)           (1시간)
  - __init__에 executor 주입
  - open_position에 executor.open_order 연동
  - close_position에 executor.close_order 연동
  - sync_balance, reconcile_positions 추가
Step 4: core_constants.py 상수 추가               (5분)
Step 5: shark_sovereign_v43.py 수정               (30분)
  - Executor 선택/생성 로직
  - sync 태스크 추가
Step 6: shark_config.json/example 업데이트         (5분)
Step 7: 테스트 작성 + 실행                         (1시간)
Step 8: 테스트넷 E2E 검증                         (사용자 수동)
```

---

## 11. 변경하지 않는 것 (명시적 경계)

| 파일 | 변경 | 이유 |
|------|------|------|
| `core_logic.py` | 없음 | 스코어링 로직 분리 원칙 |
| `core_intel.py` | 없음 | 데이터 수집 레이어 독립 |
| `shark_engine_v37.cpp` | 없음 | C++ 엔진 읽기 전용 |
| SHM 구조체 | 없음 | C++ 호환성 |
| 스코어링 계수 | 없음 | 전략 매개변수 보존 |
| 기존 테스트 | 통과 보장 | 하위 호환성 |

---

## 12. 리스크 + 완화 전략

| 리스크 | 확률 | 완화 |
|--------|------|------|
| 슬리피지로 예상 PnL과 실제 PnL 괴리 | 높음 | 실체결가 기반 PnL 계산 + 로그 |
| 네트워크 장애 중 포지션 방치 | 중간 | 30초 리콘실리에이션 + 킬 스위치 |
| 레이트 리밋 초과 | 낮음 | RateLimiter 83% 수준 제한 |
| 부분 체결 | 낮음 | 체결분 포지션 등록 + 잔량 취소 |
| 거래소 점검/장애 | 낮음 | 재시도 + 백오프 + 텔레그램 알림 |
| API 키 유출 | 낮음 | 환경변수 전용 + 로그 마스킹 |
