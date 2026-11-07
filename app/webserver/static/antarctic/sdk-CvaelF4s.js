/*!
 * @antarctic-wallet/aw-sdk v0.4.3 - dist/sdk-CvaelF4s.js
 * Vendored unmodified from the npm package (below this header the file is byte-identical to the package).
 * integrity sha512-sPkLpB5N0Y0xfo/MdA/ks7BTeiVwjuPBZoK5KMJeRiA329mfdzczfDglyMi+chkjOK6eDFFa0EJ26fOdQRPlTA==
 *
 * MIT License
 *
 * Copyright (c) 2026 Antarctic Tech
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
class K {
  constructor() {
    this.listeners = /* @__PURE__ */ new Map();
  }
  on(e, s) {
    this.listeners.has(e) || this.listeners.set(e, /* @__PURE__ */ new Set()), this.listeners.get(e).add(s);
  }
  off(e, s) {
    var i;
    (i = this.listeners.get(e)) == null || i.delete(s);
  }
  emit(e, ...s) {
    const i = this.listeners.get(e);
    if (i)
      for (const n of i)
        n(...s);
  }
  removeAllListeners() {
    this.listeners.clear();
  }
}
var h = /* @__PURE__ */ ((t) => (t.SDK_INIT = "SDK_INIT", t.SESSION_REFRESH = "SESSION_REFRESH", t.REQUEST_OPERATION_CONFIRM = "REQUEST_OPERATION_CONFIRM", t.GET_SESSION_STATUS = "GET_SESSION_STATUS", t.GET_SCOPES = "GET_SCOPES", t.GET_SCOPES_DATA = "GET_SCOPES_DATA", t.WEB_APP_SETUP_BACK_BUTTON = "web_app_setup_back_button", t.WEB_APP_REQUEST_THEME = "web_app_request_theme", t.WEB_APP_REQUEST_LANGUAGE = "web_app_request_language", t.WEB_APP_REQUEST_SAFE_AREA = "web_app_request_safe_area", t.WEB_APP_OPEN_SCAN_QR = "web_app_open_scan_qr", t.WEB_APP_TRIGGER_HAPTIC_FEEDBACK = "web_app_trigger_haptic_feedback", t.WEB_APP_SET_BACKGROUND_COLOR = "web_app_set_background_color", t.OPEN_APP = "OPEN_APP", t))(h || {}), a = /* @__PURE__ */ ((t) => (t.SDK_INIT_OK = "SDK_INIT_OK", t.SDK_INIT_FAIL = "SDK_INIT_FAIL", t.SESSION_REFRESHED = "SESSION_REFRESHED", t.SESSION_REFRESH_FAIL = "SESSION_REFRESH_FAIL", t.OPERATION_APPROVED = "OPERATION_APPROVED", t.OPERATION_REJECTED = "OPERATION_REJECTED", t.SESSION_STATUS = "SESSION_STATUS", t.SESSION_STATUS_FAIL = "SESSION_STATUS_FAIL", t.SCOPES_RESULT = "SCOPES_RESULT", t.SCOPES_DATA_RESULT = "SCOPES_DATA_RESULT", t.SCOPES_FAIL = "SCOPES_FAIL", t.BACK_BUTTON_PRESSED = "back_button_pressed", t.THEME_CHANGED = "theme_changed", t.LANGUAGE_CHANGED = "language_changed", t.SAFE_AREA_CHANGED = "safe_area_changed", t.VISIBILITY_CHANGED = "visibility_changed", t.QR_TEXT_RECEIVED = "qr_text_received", t.SCAN_QR_CLOSED = "scan_qr_closed", t.OPEN_APP_OK = "OPEN_APP_OK", t.ERROR = "ERROR", t))(a || {});
const V = "1.0";
var P = /* @__PURE__ */ ((t) => (t.Init = "init", t.SessionRefresh = "session_refresh", t.RequestConfirm = "request_confirm", t.GetSessionStatus = "get_session_status", t.GetScopes = "get_scopes", t.GetScopesData = "get_scopes_data", t.RequestTheme = "web_app_request_theme", t.RequestLanguage = "web_app_request_language", t.RequestSafeArea = "web_app_request_safe_area", t.OpenScanQr = "web_app_open_scan_qr", t.TriggerHapticFeedback = "web_app_trigger_haptic_feedback", t.SetBackgroundColor = "web_app_set_background_color", t.OpenApp = "open_app", t))(P || {});
const U = {
  init: 1,
  session_refresh: 1,
  request_confirm: 1,
  get_session_status: 1,
  get_scopes: 1,
  get_scopes_data: 1,
  web_app_request_theme: 1,
  web_app_request_language: 1,
  web_app_request_safe_area: 1,
  web_app_open_scan_qr: 1,
  web_app_trigger_haptic_feedback: 1,
  web_app_set_background_color: 1,
  open_app: 1
}, G = /* @__PURE__ */ new Set([
  "web_app_request_theme",
  "web_app_request_language",
  "web_app_request_safe_area"
  /* RequestSafeArea */
]), L = /* @__PURE__ */ new Set([
  ...G,
  "web_app_open_scan_qr",
  "web_app_trigger_haptic_feedback",
  "web_app_set_background_color",
  "open_app"
  /* OpenApp */
]);
function Ge(t) {
  if (!t) return !0;
  for (const [e, s] of Object.entries(U)) {
    if (L.has(e)) continue;
    if ((t[e] ?? 0) < s) return !1;
  }
  return !0;
}
function I(t, e) {
  const s = U[t];
  return (e[t] ?? 0) >= s;
}
class j {
  constructor(e, s) {
    this.visible = !1, this.ready = !1, this.pendingSync = !1, this.clickHandlers = /* @__PURE__ */ new Set(), this.transport = e, this.logger = s;
  }
  /**
   * Текущее состояние видимости кнопки «Назад»
   */
  get isVisible() {
    return this.visible;
  }
  /**
   * Показать кнопку «Назад» в chrome хоста
   */
  show() {
    this.visible || (this.visible = !0, this.sync());
  }
  /**
   * Скрыть кнопку «Назад» (chrome вернётся к «Закрыть»)
   */
  hide() {
    this.visible && (this.visible = !1, this.sync());
  }
  /**
   * Зарегистрировать хендлер нажатия «Назад»
   */
  onClick(e) {
    this.clickHandlers.add(e);
  }
  /**
   * Снять хендлер нажатия
   */
  offClick(e) {
    this.clickHandlers.delete(e);
  }
  /**
   * Доставка нажатия от хоста (вызывается ядром SDK по back_button_pressed)
   * @internal
   */
  handlePressed() {
    this.logger.log("BackButton: нажатие от хоста");
    for (const e of this.clickHandlers)
      e();
  }
  /**
   * Открыть канал: SDK_INIT уже ушёл (или сессия восстановлена без него) — отправить
   * накопленное состояние. До этого show()/hide() только запоминаются.
   * @internal
   */
  activate() {
    this.ready = !0, this.pendingSync && (this.pendingSync = !1, this.sync());
  }
  /**
   * Повторно отправить видимость: ретрай SDK_INIT снова сбрасывает стрелку на хосте
   * @internal
   */
  resync() {
    this.visible && this.sync();
  }
  /**
   * Сброс состояния при уничтожении SDK
   * Если кнопка была видима — шлёт hide на хост, пока транспорт ещё жив.
   * @internal
   */
  reset() {
    const e = this.visible;
    this.visible = !1, this.pendingSync = !1, this.clickHandlers.clear(), e && this.sync(), this.ready = !1;
  }
  sync() {
    if (!this.ready) {
      this.pendingSync = !0;
      return;
    }
    this.logger.log("BackButton: is_visible =", this.visible), this.transport.post(h.WEB_APP_SETUP_BACK_BUTTON, {
      is_visible: this.visible
    });
  }
}
const $ = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;
function Q(t) {
  const e = t.trim().toLowerCase();
  if (!$.test(e)) return null;
  if (e.length === 7) return e;
  const [, s, i, n] = e;
  return `#${s}${s}${i}${i}${n}${n}`;
}
class z {
  constructor(e, s) {
    this.transport = e, this.logger = s;
  }
  /** Последний принятый цвет (#rrggbb) */
  get backgroundColor() {
    return this.color;
  }
  /** Принимает #rgb | #rrggbb; невалидный цвет не отправляется — возвращает false */
  setBackgroundColor(e) {
    const s = Q(e);
    return s ? (this.color = s, this.logger.log("Background color:", s), this.transport.post(h.WEB_APP_SET_BACKGROUND_COLOR, {
      color: s
    }), !0) : (this.logger.warn("setBackgroundColor: ожидается #rgb или #rrggbb, получено", e), !1);
  }
  reset() {
    this.color = void 0;
  }
}
const C = "--aw-";
function J(t) {
  return t === "web" || t === "tma" || t === "ios" || t === "android" ? t : void 0;
}
function B(t) {
  return t === "light" || t === "dark" ? t : void 0;
}
function F(t) {
  if (!(typeof t != "string" || t.length > 64))
    try {
      return Intl.getCanonicalLocales(t)[0];
    } catch {
      return;
    }
}
function X(t) {
  return typeof t == "boolean" ? t : t === "true" ? !0 : t === "false" ? !1 : void 0;
}
function A(t) {
  return typeof t == "number" && Number.isFinite(t) && t >= 0;
}
function Y(t) {
  if (!t || typeof t != "object") return;
  const { top: e, right: s, bottom: i, left: n } = t;
  if (!(!A(e) || !A(s) || !A(i) || !A(n)))
    return Object.freeze({ top: e, right: s, bottom: i, left: n });
}
function Z() {
  if (typeof window > "u") return { isActive: !0 };
  const t = new URLSearchParams(window.location.search);
  return {
    platform: J(t.get("awPlatform")),
    colorScheme: B(t.get("awColorScheme")),
    languageCode: F(t.get("awLanguageCode")),
    isActive: !0
  };
}
function k(t) {
  var i, n, r, o;
  if (typeof document > "u") return;
  const e = document.documentElement.style, s = (p, c) => {
    c === void 0 ? e.removeProperty(C + p) : e.setProperty(C + p, typeof c == "number" ? `${c}px` : c);
  };
  s("safe-area-inset-top", (i = t.safeAreaInset) == null ? void 0 : i.top), s("safe-area-inset-right", (n = t.safeAreaInset) == null ? void 0 : n.right), s("safe-area-inset-bottom", (r = t.safeAreaInset) == null ? void 0 : r.bottom), s("safe-area-inset-left", (o = t.safeAreaInset) == null ? void 0 : o.left), s("color-scheme", t.colorScheme);
}
const ee = [
  h.WEB_APP_REQUEST_THEME,
  h.WEB_APP_REQUEST_LANGUAGE,
  h.WEB_APP_REQUEST_SAFE_AREA
];
class te {
  constructor(e, s, i) {
    this.transport = e, this.events = s, this.appId = i, this.state = Z(), this.listening = !1, this.received = !1, this.handleMessage = (n) => {
      if (n.appId !== this.appId) return;
      const r = n.payload && typeof n.payload == "object" ? n.payload : null;
      if (r)
        switch (n.type) {
          case a.THEME_CHANGED:
            return this.apply({ colorScheme: B(r.color_scheme) }, "themeChanged");
          case a.LANGUAGE_CHANGED:
            return this.apply({ languageCode: F(r.language_code) }, "languageChanged");
          case a.SAFE_AREA_CHANGED:
            return this.apply({ safeAreaInset: Y(r) }, "safeAreaChanged");
          case a.VISIBILITY_CHANGED: {
            const o = X(r.is_visible);
            if (o === void 0) return;
            const p = this.state.isActive !== o;
            this.state = { ...this.state, isActive: o }, this.received = !0, p && this.events.emit(o ? "activated" : "deactivated");
            return;
          }
        }
    }, k(this.state);
  }
  get platform() {
    return this.state.platform;
  }
  get colorScheme() {
    return this.state.colorScheme;
  }
  get languageCode() {
    return this.state.languageCode;
  }
  get safeAreaInset() {
    return this.state.safeAreaInset;
  }
  get isActive() {
    return this.state.isActive;
  }
  /** Хост ответил хотя бы на один запрос — команды окружения поддерживаются */
  get isSupported() {
    return this.received;
  }
  start() {
    this.listening || (this.transport.on(this.handleMessage), this.listening = !0);
  }
  /** Запросить у хоста свежий снимок; старый хост запросы игнорирует, ответа не ждём */
  refresh() {
    for (const e of ee) this.transport.post(e, {});
  }
  destroy() {
    this.transport.off(this.handleMessage), this.listening = !1, this.received = !1, this.state = { isActive: !0 }, k(this.state);
  }
  apply(e, s) {
    const i = Object.entries(e).filter(([, r]) => r !== void 0);
    i.length === 0 || (this.received = !0, !i.some(([r, o]) => JSON.stringify(this.state[r]) !== JSON.stringify(o))) || (this.state = { ...this.state, ...Object.fromEntries(i) }, k(this.state), this.events.emit(s));
  }
}
var g = /* @__PURE__ */ ((t) => (t.ScopesRejected = "scopes_rejected", t.InvalidAppId = "invalid_app_id", t.InvalidOrigin = "invalid_origin", t.Timeout = "timeout", t.AlreadyInitialized = "already_initialized", t.GenericError = "generic_error", t))(g || {});
const se = {
  scopes_rejected: "User rejected the requested scopes.",
  invalid_app_id: "The app ID is not registered or invalid.",
  invalid_origin: "The origin is not allowed for this app.",
  timeout: "Initialization timed out. Make sure the app is running inside Antarctic Wallet.",
  already_initialized: "SDK is already initialized.",
  generic_error: "Something unexpected went wrong during initialization."
};
var S = /* @__PURE__ */ ((t) => (t.Expired = "session_expired", t.Revoked = "session_revoked", t.RefreshFailed = "refresh_failed", t.InvalidToken = "invalid_token", t.GenericError = "generic_error", t))(S || {});
const ie = {
  session_expired: "Session has expired. Please re-initialize the SDK.",
  session_revoked: "Session has been revoked by the user or wallet.",
  refresh_failed: "Failed to refresh the session token.",
  invalid_token: "The session token is invalid.",
  generic_error: "Something unexpected went wrong with the session."
};
var b = /* @__PURE__ */ ((t) => (t.UserRejected = "user_rejected", t.InsufficientBalance = "insufficient_balance", t.InvalidRecipient = "invalid_recipient", t.InvalidAmount = "invalid_amount", t.TransactionFailed = "transaction_failed", t.PreparationFailed = "preparation_failed", t.ConfirmationTimeout = "confirmation_timeout", t.GenericError = "generic_error", t))(b || {});
const ne = {
  user_rejected: "User rejected the operation.",
  insufficient_balance: "Insufficient balance to complete this operation.",
  invalid_recipient: "The recipient address is invalid.",
  invalid_amount: "The amount is invalid or below minimum.",
  transaction_failed: "The transaction failed. Please try again.",
  preparation_failed: "Failed to prepare the operation.",
  confirmation_timeout: "Operation confirmation timed out.",
  generic_error: "Something unexpected went wrong with the operation."
};
var E = /* @__PURE__ */ ((t) => (t.Rejected = "scopes_rejected", t.InvalidScope = "invalid_scope", t.NotGranted = "scope_not_granted", t.GenericError = "generic_error", t))(E || {}), _ = /* @__PURE__ */ ((t) => (t.Unsupported = "unsupported", t.Closed = "closed", t.NotActive = "not_active", t.AlreadyOpen = "already_open", t.CameraDenied = "camera_denied", t.GenericError = "generic_error", t))(_ || {});
const re = {
  unsupported: "The host does not support the QR scanner.",
  closed: "The user closed the scanner.",
  not_active: "The app must be on screen to open the scanner.",
  already_open: "The scanner is already open.",
  camera_denied: "Camera access was denied.",
  generic_error: "Something unexpected went wrong with the scanner."
};
var d = /* @__PURE__ */ ((t) => (t.NotFound = "not_found", t.Blocked = "blocked", t.Suspended = "suspended", t.StackLimit = "stack_limit", t.RateLimited = "rate_limited", t.InvalidAppId = "invalid_app_id", t.SameApp = "same_app", t.InvalidStartParam = "invalid_start_param", t.InvalidFullscreen = "invalid_fullscreen", t.Unsupported = "unsupported", t.GenericError = "generic_error", t))(d || {});
const oe = {
  not_found: "The app was not found.",
  blocked: "The app is blocked.",
  suspended: "The app is suspended.",
  stack_limit: "The app stack is full.",
  rate_limited: "Too many app opens. Try again later.",
  invalid_app_id: "appId must be a non-empty string after trimming.",
  same_app: "Cannot open the current app.",
  invalid_start_param: "startParam must be 1–512 characters from A-Za-z0-9_-.",
  invalid_fullscreen: "fullscreen must be a boolean.",
  unsupported: "The host does not support opening another app.",
  generic_error: "Something unexpected went wrong while opening the app."
}, ae = {
  scopes_rejected: "User rejected the scope request.",
  invalid_scope: "One or more requested scopes are invalid.",
  scope_not_granted: "The required scope has not been granted.",
  generic_error: "Something unexpected went wrong with scope management."
}, ce = "0.4.3";
class u extends Error {
  constructor(e, s) {
    super(s), this.name = "AWSDKError", this.code = e;
  }
}
class M extends u {
  constructor(e) {
    super(g.Timeout, `Request ${e} timed out`), this.name = "AWTimeoutError";
  }
}
class O extends u {
  constructor(e, s) {
    super(e, s ?? se[e] ?? "Initialization failed."), this.name = "AWInitError", this.errorCode = e;
  }
}
class m extends u {
  constructor(e, s) {
    super(e, s ?? ie[e] ?? "Session error."), this.name = "AWSessionError", this.errorCode = e;
  }
}
class w extends u {
  constructor(e, s, i) {
    super(s, i ?? ne[s] ?? "Operation failed."), this.name = "AWOperationError", this.operationId = e, this.errorCode = s;
  }
}
class y extends u {
  constructor(e, s) {
    super(e, s ?? ae[e] ?? "Scope error."), this.name = "AWScopeError", this.errorCode = e;
  }
}
class f extends u {
  constructor(e, s) {
    super(e, s ?? re[e]), this.name = "AWScanQrError", this.errorCode = e;
  }
}
class l extends u {
  constructor(e, s) {
    super(e, s ?? oe[e]), this.name = "AWOpenAppError", this.errorCode = e;
  }
}
const he = 16384, pe = 256, de = 2048;
function T(t) {
  const e = le(t);
  if (!e) return {};
  const s = _e(e.sub), i = ge(e.userImage);
  return {
    ...s ? { displayName: s } : {},
    ...i ? { avatarUrl: i } : {}
  };
}
function le(t) {
  if (!t || t.length > he) return null;
  const e = t.split(".");
  if (e.length !== 3 || !e[1]) return null;
  try {
    const s = ue(e[1]), i = JSON.parse(s);
    return !i || typeof i != "object" || Array.isArray(i) ? null : i;
  } catch {
    return null;
  }
}
function ue(t) {
  const e = t.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(t.length / 4) * 4, "="), s = Uint8Array.from(atob(e), (i) => i.charCodeAt(0));
  return new TextDecoder().decode(s);
}
function _e(t) {
  if (typeof t != "string") return;
  const e = t.trim();
  if (!(!e || e.length > pe))
    return e;
}
function ge(t) {
  if (typeof t != "string") return;
  const e = t.trim();
  if (!(!e || e.length > de))
    try {
      return new URL(e).protocol !== "https:" ? void 0 : e;
    } catch {
      return;
    }
}
const q = /^[A-Za-z0-9_-]{1,512}$/;
function H(t) {
  return t == null || t === "" ? null : typeof t == "string" && q.test(t) ? t : (console.warn("[AW-SDK]", "Невалидный start_param отброшен:", t), null);
}
async function fe(t, e, s, i, n, r) {
  const o = {
    appId: e,
    origin: window.location.origin,
    sdkVersion: ce,
    requestedScopes: s
  };
  n.log("Хэндшейк начат, ожидание ответа...");
  const p = await t.sendAndWait(
    h.SDK_INIT,
    o,
    i,
    r
  );
  if (p.type === a.SDK_INIT_OK) {
    const c = p.payload;
    return n.log("Хэндшейк успешен, скоупы:", c.grantedScopes), {
      session: {
        sessionToken: c.sessionToken,
        idToken: c.idToken ?? null,
        grantedScopes: c.grantedScopes,
        userContext: T(c.idToken),
        expiresAt: c.expiresAt
      },
      supportedCommands: c.supportedCommands,
      startParam: H(c.start_param)
    };
  }
  if (p.type === a.SDK_INIT_FAIL) {
    const c = p.payload;
    throw new O(
      c.code || g.GenericError,
      c.message
    );
  }
  throw new O(g.GenericError, `Unexpected response type: ${p.type}`);
}
class Se {
  constructor(e, s) {
    this.transport = e, this.logger = s;
  }
  /** Удар: light | medium | heavy | rigid | soft */
  impactOccurred(e) {
    this.trigger({ type: "impact", impact_style: e });
  }
  /** Результат действия: success | warning | error */
  notificationOccurred(e) {
    this.trigger({ type: "notification", notification_type: e });
  }
  /** Смена выбора (переключатель, пикер) */
  selectionChanged() {
    this.trigger({ type: "selection_change" });
  }
  trigger(e) {
    this.logger.log("Haptic:", e.type), this.transport.post(h.WEB_APP_TRIGGER_HAPTIC_FEEDBACK, e);
  }
}
const Ee = new Set(Object.values(d));
class Ae {
  constructor(e, s, i, n, r) {
    this.transport = e, this.timeout = s, this.logger = i, this.currentAppId = n, this.isSupported = r;
  }
  async open(e) {
    const s = me(e, this.currentAppId);
    if (!this.isSupported()) throw new l(d.Unsupported);
    this.logger.log("OpenApp:", s.appId);
    const i = await this.transport.sendAndWait(
      h.OPEN_APP,
      s,
      this.timeout,
      { maxAttempts: 1, baseDelay: 0 }
    );
    if (i.type !== a.OPEN_APP_OK) {
      if (i.type === a.ERROR) {
        const n = i.payload, r = Ee.has(n == null ? void 0 : n.code) ? n.code : d.GenericError;
        throw new l(r, n == null ? void 0 : n.message);
      }
      throw new l(d.GenericError, `Unexpected response: ${i.type}`);
    }
  }
}
function me(t, e) {
  if (typeof (t == null ? void 0 : t.appId) != "string")
    throw new l(d.InvalidAppId);
  const s = t.appId.trim();
  if (s.length === 0)
    throw new l(d.InvalidAppId);
  if (s === e)
    throw new l(d.SameApp);
  const { startParam: i, fullscreen: n } = t;
  if (i != null && i !== "" && (typeof i != "string" || !q.test(i)))
    throw new l(d.InvalidStartParam);
  if (n !== void 0 && typeof n != "boolean")
    throw new l(d.InvalidFullscreen);
  return {
    appId: s,
    ...typeof i == "string" && i !== "" ? { startParam: i } : {},
    ...typeof n == "boolean" ? { fullscreen: n } : {}
  };
}
class we {
  constructor(e, s, i) {
    this.transport = e, this.timeout = s, this.logger = i;
  }
  /**
   * Запрос подтверждения операции: хост показывает нативный лист, approve + commit.
   * Retry не применяется — идёт ожидание действия пользователя.
   *
   * @param operationId id операции, полученный от вашего бэкенда
   */
  async requestConfirmation(e) {
    this.logger.log("Запрос подтверждения для:", e);
    try {
      const s = await this.transport.sendAndWait(h.REQUEST_OPERATION_CONFIRM, { operationId: e }, this.timeout);
      if (s.type === a.OPERATION_REJECTED) {
        const i = s.payload;
        throw new w(e, b.UserRejected, i.reason);
      }
      if (s.type === a.OPERATION_APPROVED) {
        const i = s.payload;
        return this.logger.log("Операция подтверждена:", i.operationId), {
          operationId: i.operationId,
          status: i.status,
          txId: i.txId
        };
      }
      throw new w(
        e,
        b.GenericError,
        `Unexpected response: ${s.type}`
      );
    } catch (s) {
      throw s instanceof w ? s : new w(
        e,
        b.GenericError,
        s.message
      );
    }
  }
}
const ye = 600 * 1e3, Re = new Set(Object.values(_));
class be {
  constructor(e, s, i) {
    this.transport = e, this.logger = s, this.isSupported = i, this.pending = !1;
  }
  get isOpen() {
    return this.pending;
  }
  async open() {
    if (!this.isSupported()) throw new f(_.Unsupported);
    if (this.pending) throw new f(_.AlreadyOpen);
    this.pending = !0, this.logger.log("ScanQr: открываем сканер хоста");
    try {
      const e = await this.transport.sendAndWait(
        h.WEB_APP_OPEN_SCAN_QR,
        {},
        ye,
        { maxAttempts: 1, baseDelay: 0 }
      );
      if (e.type === a.QR_TEXT_RECEIVED) {
        const { data: n } = e.payload;
        if (typeof n != "string") throw new f(_.GenericError);
        return n;
      }
      if (e.type === a.SCAN_QR_CLOSED)
        throw new f(_.Closed);
      const s = e.payload, i = Re.has(s == null ? void 0 : s.code) ? s.code : _.GenericError;
      throw new f(i, s == null ? void 0 : s.message);
    } finally {
      this.pending = !1;
    }
  }
}
class Te {
  constructor(e, s, i, n) {
    this.transport = e, this.timeout = s, this.logger = i, this.retry = n;
  }
  /**
   * Получить текущие скоупы
   */
  async getScopes() {
    this.logger.log("Запрос скоупов...");
    const e = await this.transport.sendAndWait(h.GET_SCOPES, {}, this.timeout, this.retry);
    if (e.type === a.SCOPES_RESULT)
      return e.payload.grantedScopes;
    if (e.type === a.SCOPES_FAIL) {
      const s = e.payload;
      throw new y(
        s.code || E.GenericError,
        s.message
      );
    }
    throw new y(E.GenericError, `Unexpected response: ${e.type}`);
  }
  /**
   * Получить данные по скоупам
   */
  async getData() {
    this.logger.log("Запрос данных скоупов...");
    const e = await this.transport.sendAndWait(h.GET_SCOPES_DATA, {}, this.timeout, this.retry);
    if (e.type === a.SCOPES_DATA_RESULT)
      return e.payload.data;
    if (e.type === a.SCOPES_FAIL) {
      const s = e.payload;
      throw new y(
        s.code || E.GenericError,
        s.message
      );
    }
    throw new y(E.GenericError, `Unexpected response: ${e.type}`);
  }
}
class Oe {
  constructor(e, s, i, n) {
    this.autoRefreshTimer = null, this.transport = e, this.timeout = s, this.logger = i, this.retry = n;
  }
  /**
   * Запросить обновление сессии у родителя
   */
  async refresh() {
    this.logger.log("Запрос обновления сессии...");
    const e = await this.transport.sendAndWait(h.SESSION_REFRESH, {}, this.timeout, this.retry);
    if (e.type === a.SESSION_REFRESHED) {
      const s = e.payload;
      return this.logger.log("Сессия обновлена, истекает:", new Date(s.expiresAt).toISOString()), s;
    }
    if (e.type === a.SESSION_REFRESH_FAIL) {
      const s = e.payload;
      throw new m(
        s.code || S.RefreshFailed,
        s.message
      );
    }
    throw new m(S.GenericError, `Unexpected response: ${e.type}`);
  }
  /**
   * Получить статус сессии через родителя
   */
  async getStatus() {
    this.logger.log("Запрос статуса сессии...");
    const e = await this.transport.sendAndWait(h.GET_SESSION_STATUS, {}, this.timeout, this.retry);
    if (e.type === a.SESSION_STATUS) {
      const s = e.payload, { start_param: i, ...n } = s;
      return { ...n, startParam: H(i) };
    }
    if (e.type === a.SESSION_STATUS_FAIL) {
      const s = e.payload;
      throw new m(
        s.code || S.GenericError,
        s.message
      );
    }
    throw new m(S.GenericError, `Unexpected response: ${e.type}`);
  }
  /**
   * Запуск таймера авто-рефреша (обновление за 60с до истечения)
   */
  startAutoRefresh(e, s, i) {
    this.stopAutoRefresh();
    const n = e - Date.now(), r = Math.max(n - 6e4, 0);
    this.logger.log("Авто-рефреш запланирован через", Math.round(r / 1e3), "секунд"), this.autoRefreshTimer = setTimeout(async () => {
      try {
        const o = await this.refresh();
        s(o), this.startAutoRefresh(o.expiresAt, s, i);
      } catch (o) {
        this.logger.error("Авто-рефреш не удался:", o), i == null || i();
      }
    }, r);
  }
  /**
   * Остановить таймер авто-рефреша
   */
  stopAutoRefresh() {
    this.autoRefreshTimer && (clearTimeout(this.autoRefreshTimer), this.autoRefreshTimer = null);
  }
  destroy() {
    this.stopAutoRefresh();
  }
}
function ve(t) {
  if (!t || typeof t != "object") return !1;
  const e = t;
  return typeof e.type == "string" && typeof e.requestId == "string" && typeof e.version == "string" && typeof e.appId == "string" && typeof e.timestamp == "number" && "payload" in e;
}
function Ie(t, e, s, i, n) {
  return {
    type: t,
    requestId: i,
    payload: e,
    version: n,
    appId: s,
    timestamp: Date.now()
  };
}
function ke() {
  return crypto.randomUUID();
}
const Ne = {
  maxAttempts: 1,
  baseDelay: 1e3
};
async function Pe(t, e, s) {
  let i;
  for (let n = 1; n <= e.maxAttempts; n++)
    try {
      return await t();
    } catch (r) {
      if (!(r instanceof M))
        throw r;
      if (i = r, n < e.maxAttempts) {
        const o = e.baseDelay * Math.pow(2, n - 1);
        s == null || s.warn(`Таймаут, retry ${n}/${e.maxAttempts} через ${o}мс...`), await Ce(o);
      }
    }
  throw i;
}
function Ce(t) {
  return new Promise((e) => setTimeout(e, t));
}
class De {
  constructor(e, s, i) {
    this.handlers = /* @__PURE__ */ new Set(), this.pendingRequests = /* @__PURE__ */ new Map(), this.boundListener = null, this.parentOrigin = e, this.appId = s, this.logger = i;
  }
  /**
   * Начать слушать входящие сообщения
   */
  init() {
    this.boundListener = this.handleEvent.bind(this), window.addEventListener("message", this.boundListener), this.logger.log("Транспорт инициализирован, слушаем сообщения от", this.parentOrigin);
  }
  /**
   * Отправить сообщение в родительское окно
   */
  send(e, s, i) {
    var o;
    const n = i ?? ke(), r = Ie(e, s, this.appId, n, V);
    return this.logger.log("Отправка сообщения:", e, n), (o = window.ReactNativeWebView) != null && o.postMessage ? (window.ReactNativeWebView.postMessage(JSON.stringify(r)), n) : (window.parent && window.parent !== window && window.parent.postMessage(r, this.parentOrigin), n);
  }
  /**
   * Отправить fire-and-forget сообщение без ожидания ответа (например, setup BackButton)
   */
  post(e, s) {
    this.send(e, s);
  }
  /**
   * Ожидание ответа по ID запроса
   */
  waitForResponse(e, s = 3e4) {
    return new Promise((i, n) => {
      const r = setTimeout(() => {
        this.off(o), this.pendingRequests.delete(e), n(new M(e));
      }, s), o = (p) => {
        p.requestId === e && (clearTimeout(r), this.off(o), this.pendingRequests.delete(e), i(p));
      };
      this.pendingRequests.set(e, { timer: r, reject: n }), this.on(o);
    });
  }
  /**
   * Отправить сообщение и ждать ответ с retry при таймауте.
   * Каждая попытка — новый requestId + новый send + новый waitForResponse.
   */
  sendAndWait(e, s, i, n = Ne) {
    return Pe(
      () => {
        const r = this.send(e, s);
        return this.waitForResponse(r, i);
      },
      n,
      this.logger
    );
  }
  /**
   * Регистрация обработчика сообщений
   */
  on(e) {
    this.handlers.add(e);
  }
  /**
   * Удаление обработчика сообщений
   */
  off(e) {
    this.handlers.delete(e);
  }
  /**
   * Очистка всех слушателей
   */
  destroy() {
    this.boundListener && (window.removeEventListener("message", this.boundListener), this.boundListener = null), this.handlers.clear();
    for (const { timer: e, reject: s } of this.pendingRequests.values())
      clearTimeout(e), s(new u(g.GenericError, "Transport destroyed"));
    this.pendingRequests.clear(), this.logger.log("Транспорт уничтожен");
  }
  handleEvent(e) {
    if (e.origin !== this.parentOrigin || !ve(e.data))
      return;
    const s = e.data;
    this.logger.log("Получено сообщение:", s.type, s.requestId);
    const i = { isReply: this.pendingRequests.has(s.requestId) };
    for (const n of this.handlers)
      n(s, i);
  }
}
class xe {
  constructor(e, s = !1) {
    this.prefix = `[${e}]`, this.enabled = s;
  }
  log(...e) {
    this.enabled && console.log(this.prefix, ...e);
  }
  warn(...e) {
    this.enabled && console.warn(this.prefix, ...e);
  }
  error(...e) {
    console.error(this.prefix, ...e);
  }
}
const v = "aw-sdk:session:";
function N(t, e, s, i) {
  try {
    const n = v + t, r = {
      session: e,
      savedAt: Date.now(),
      supportedCommands: i ?? W(t)
    };
    sessionStorage.setItem(n, JSON.stringify(r)), s == null || s.log("Сессия сохранена в sessionStorage");
  } catch {
    s == null || s.warn("Не удалось сохранить сессию в sessionStorage");
  }
}
function Ue(t, e) {
  try {
    const s = v + t, i = sessionStorage.getItem(s);
    if (!i) return null;
    const n = JSON.parse(i);
    return n.session.expiresAt <= Date.now() ? (e == null || e.log("Сохранённая сессия истекла, удаляем"), sessionStorage.removeItem(s), null) : (e == null || e.log("Сессия загружена из sessionStorage"), n.session);
  } catch {
    return e == null || e.warn("Не удалось загрузить сессию из sessionStorage"), null;
  }
}
function W(t) {
  try {
    const e = sessionStorage.getItem(v + t);
    return e ? JSON.parse(e).supportedCommands ?? {} : {};
  } catch {
    return {};
  }
}
function R(t, e) {
  try {
    sessionStorage.removeItem(v + t), e == null || e.log("Сессия удалена из sessionStorage");
  } catch {
    e == null || e.warn("Не удалось удалить сессию из sessionStorage");
  }
}
const D = 3e4, x = { maxAttempts: 3, baseDelay: 1e3 };
class Le {
  constructor(e) {
    var i, n;
    this.sessionToken = null, this.session = null, this.startParamValue = null, this.startParamCaptured = !1, this.initialized = !1, this.supportedCommands = {}, this.events = new K(), this.config = e, this.logger = new xe("AW-SDK", e.debug ?? !1), this.transport = new De(e.parentOrigin, e.appId, this.logger), this.persistEnabled = e.persistSession ?? !0;
    const s = e.timeout ?? D;
    this.retryConfig = {
      maxAttempts: ((i = e.retry) == null ? void 0 : i.maxAttempts) ?? x.maxAttempts,
      baseDelay: ((n = e.retry) == null ? void 0 : n.baseDelay) ?? x.baseDelay
    }, this.operations = new we(this.transport, s, this.logger), this.sessionModule = new Oe(
      this.transport,
      s,
      this.logger,
      this.retryConfig
    ), this.scopes = new Te(this.transport, s, this.logger, this.retryConfig), this.backButton = new j(this.transport, this.logger), this.hapticFeedback = new Se(this.transport, this.logger), this.environmentModule = new te(this.transport, this.events, e.appId), this.scanQrModule = new be(this.transport, this.logger, () => this.isCommandAvailable(P.OpenScanQr)), this.openAppModule = new Ae(
      this.transport,
      s,
      this.logger,
      e.appId,
      () => this.isCommandAvailable(P.OpenApp)
    ), this.backgroundModule = new z(this.transport, this.logger);
  }
  // ============================================================================
  // Static Methods
  // ============================================================================
  /**
   * Проверяет, запущено ли приложение внутри Antarctic Wallet (iframe или WebView)
   */
  static isInsideWallet() {
    return typeof window > "u" ? !1 : !!(window.parent !== window || window.ReactNativeWebView);
  }
  // ============================================================================
  // User State
  // ============================================================================
  /**
   * Контекст текущего пользователя (доступен после init).
   * displayName — это `sub` (per-app login), его и показываем как имя.
   */
  get user() {
    var e;
    return ((e = this.session) == null ? void 0 : e.userContext) ?? null;
  }
  // ============================================================================
  // Start parameter
  // ============================================================================
  /**
   * Стартовый параметр этого открытия (start_param).
   * null до первого успешного init(), если хост его не передал, или если значение пустое / невалидное.
   * Повторный init() и refreshSession() его не меняют. destroy() сбрасывает.
   * Не читается из URL и не входит в launch-env и сессию.
   */
  get startParam() {
    return this.startParamValue;
  }
  // ============================================================================
  // Host Environment
  // ============================================================================
  /** Платформа хоста: web | tma | ios | android (из launch URL) */
  get platform() {
    return this.environmentModule.platform;
  }
  /** Применённая тема хоста */
  get colorScheme() {
    return this.environmentModule.colorScheme;
  }
  /** Язык интерфейса кошелька, BCP 47 (расширение AW) */
  get languageCode() {
    return this.environmentModule.languageCode;
  }
  /** Безопасные отступы внутри контейнера */
  get safeAreaInset() {
    return this.environmentModule.safeAreaInset;
  }
  /** Приложение на экране: не свёрнуто, кошелёк на переднем плане */
  get isActive() {
    return this.environmentModule.isActive;
  }
  /**
   * Запросить у хоста свежий снимок окружения (тема, язык, safe-area).
   * Обычно не нужен: хост сам шлёт снимок перед завершением init() и при каждом изменении.
   */
  refreshEnvironment() {
    this.environmentModule.refresh();
  }
  // ============================================================================
  // Background color
  // ============================================================================
  /**
   * Цвет фона под страницей приложения (#rgb | #rrggbb). Кошелёк красит им контейнер
   * webview, чтобы при оверскролле не был виден его собственный тон.
   * Без ответа; старый кошелёк игнорирует. Возвращает false для невалидного цвета.
   */
  setBackgroundColor(e) {
    return this.backgroundModule.setBackgroundColor(e);
  }
  /** Последний принятый цвет фона (#rrggbb) */
  get backgroundColor() {
    return this.backgroundModule.backgroundColor;
  }
  // ============================================================================
  // QR Scanner
  // ============================================================================
  /**
   * Открыть QR-сканер кошелька и получить распознанную строку.
   * Камера остаётся у кошелька; сканер закрывается после первого результата.
   * Отклоняется AWScanQrError: closed (пользователь закрыл), not_active, already_open,
   * camera_denied, unsupported (старый кошелёк).
   */
  scanQr() {
    return this.scanQrModule.open();
  }
  /**
   * Открыть зарегистрированную мини-апку внутри кошелька.
   * startParam — до 512 символов из A-Za-z0-9_-; пустая строка, null и отсутствие не отправляются.
   * appId обрезается по краям до проверки; пустая после trim строка — invalid_app_id.
   * Свой appId запрещён и на хост не уходит.
   * Отклоняется AWOpenAppError: not_found, blocked, suspended, stack_limit, rate_limited,
   * invalid_app_id, same_app, invalid_start_param, unsupported (старый кошелёк).
   */
  openApp(e) {
    return this.openAppModule.open(e);
  }
  /**
   * Подписанный OIDC id_token текущей сессии (доступен после init).
   * Форвардь его на свой бэкенд: тот проверит подпись через JWKS хоста и достанет
   * доверенный user_id из sub. Для авторизации используй ТОЛЬКО его, не userContext.
   */
  get idToken() {
    var e;
    return ((e = this.session) == null ? void 0 : e.idToken) ?? null;
  }
  // ============================================================================
  // Command Versioning
  // ============================================================================
  /**
   * Проверить, доступна ли конкретная команда на хосте
   */
  isCommandAvailable(e) {
    return G.has(e) ? this.environmentModule.isSupported || I(e, this.supportedCommands) : L.has(e) ? I(e, this.supportedCommands) : Object.keys(this.supportedCommands).length === 0 ? !0 : I(e, this.supportedCommands);
  }
  // ============================================================================
  // Initialization
  // ============================================================================
  /**
   * Инициализация SDK: запуск транспорта, хэндшейк, авто-рефреш.
   * При наличии сохранённой сессии — восстанавливает её без полного хэндшейка.
   */
  async init() {
    if (this.initialized)
      return this.logger.warn("SDK уже инициализирован"), this.session;
    this.transport.init(), this.setupGlobalErrorHandler(), this.environmentModule.start();
    const e = await this.tryRestoreSession();
    return e || this.performFullInit();
  }
  /** Первый успешный init фиксирует параметр до destroy(). */
  acceptStartParam(e) {
    this.startParamCaptured || (this.startParamCaptured = !0, this.startParamValue = e);
  }
  /**
   * Попытка восстановить сессию из sessionStorage.
   * Если сессия есть и валидна — возвращает AWSession, иначе null.
   */
  async tryRestoreSession() {
    if (!this.persistEnabled) return null;
    const e = Ue(this.config.appId, this.logger);
    if (!e) return null;
    try {
      const s = await this.sessionModule.getStatus();
      if (s.status === "active") {
        this.logger.log("Сессия восстановлена из sessionStorage");
        const i = s.idToken ?? e.idToken ?? null;
        return this.session = {
          sessionToken: e.sessionToken,
          idToken: i,
          grantedScopes: s.grantedScopes,
          expiresAt: s.expiresAt,
          userContext: T(i)
        }, this.sessionToken = e.sessionToken, this.supportedCommands = W(this.config.appId), this.acceptStartParam(s.startParam), this.initialized = !0, this.backButton.activate(), this.startAutoRefreshAndEmit(this.session), this.session;
      }
      return this.logger.log("Сохранённая сессия неактивна:", s.status), R(this.config.appId, this.logger), null;
    } catch {
      return this.logger.warn("Не удалось валидировать сохранённую сессию, полный хэндшейк"), R(this.config.appId, this.logger), null;
    }
  }
  /**
   * Полный хэндшейк с retry
   */
  async performFullInit() {
    try {
      const e = fe(
        this.transport,
        this.config.appId,
        this.config.scopes,
        this.config.timeout ?? D,
        this.logger,
        this.retryConfig
      );
      this.backButton.activate();
      const s = await e;
      return this.session = s.session, this.sessionToken = s.session.sessionToken, this.supportedCommands = s.supportedCommands ?? {}, this.acceptStartParam(s.startParam), this.initialized = !0, this.backButton.resync(), this.persistEnabled && N(this.config.appId, s.session, this.logger, this.supportedCommands), this.startAutoRefreshAndEmit(s.session), s.session;
    } catch (e) {
      if (e instanceof O)
        throw this.events.emit("sdk.error", {
          code: e.errorCode,
          message: e.message
        }), e;
      const s = e instanceof Error ? e : new Error(String(e));
      throw this.events.emit("sdk.error", {
        code: g.GenericError,
        message: s.message
      }), new O(g.GenericError, s.message);
    }
  }
  /**
   * Запуск авто-рефреша и эмит событий
   */
  startAutoRefreshAndEmit(e) {
    this.sessionModule.startAutoRefresh(
      e.expiresAt,
      (s) => {
        if (this.sessionToken = s.sessionToken, this.session) {
          const i = s.idToken ?? this.session.idToken ?? null;
          this.session = {
            ...this.session,
            sessionToken: s.sessionToken,
            idToken: i,
            grantedScopes: s.grantedScopes,
            expiresAt: s.expiresAt,
            userContext: T(i)
          }, this.persistEnabled && N(this.config.appId, this.session, this.logger);
        }
        this.events.emit("session.refreshed", {
          sessionToken: s.sessionToken,
          idToken: s.idToken ?? null,
          expiresAt: s.expiresAt
        });
      },
      () => {
        this.session = null, this.sessionToken = null, this.initialized = !1, this.persistEnabled && R(this.config.appId, this.logger), this.events.emit("session.expired");
      }
    ), this.events.emit("sdk.ready", e), this.events.emit("scopes.granted", { scopes: e.grantedScopes });
  }
  /**
   * Получить статус сессии с бэкенда
   */
  async status() {
    const { status: e, idToken: s, grantedScopes: i, expiresAt: n } = await this.sessionModule.getStatus();
    return { status: e, idToken: s, grantedScopes: i, expiresAt: n };
  }
  /**
   * Ручное обновление сессии
   */
  async refreshSession() {
    const e = await this.sessionModule.refresh();
    if (this.sessionToken = e.sessionToken, this.session) {
      const s = e.idToken ?? this.session.idToken ?? null;
      this.session = {
        ...this.session,
        sessionToken: e.sessionToken,
        idToken: s,
        grantedScopes: e.grantedScopes,
        expiresAt: e.expiresAt,
        userContext: T(s)
      }, this.persistEnabled && N(this.config.appId, this.session, this.logger);
    }
    this.events.emit("session.refreshed", {
      sessionToken: e.sessionToken,
      idToken: e.idToken ?? null,
      expiresAt: e.expiresAt
    });
  }
  /**
   * Получить текущую сессию
   */
  getSession() {
    return this.session;
  }
  /**
   * Очистка всех ресурсов
   */
  destroy() {
    this.backButton.reset(), this.backgroundModule.reset(), this.environmentModule.destroy(), this.sessionModule.destroy(), this.transport.destroy(), this.events.removeAllListeners(), this.initialized = !1, this.session = null, this.sessionToken = null, this.startParamValue = null, this.startParamCaptured = !1, this.supportedCommands = {}, this.persistEnabled && R(this.config.appId, this.logger), this.logger.log("SDK уничтожен");
  }
  setupGlobalErrorHandler() {
    this.transport.on((e, s) => {
      if (e.type === a.ERROR && !s.isReply) {
        const i = e.payload;
        this.logger.error("Ошибка от родителя:", i), this.events.emit("sdk.error", { code: i.code, message: i.message });
      }
      if (e.type === a.OPERATION_REJECTED) {
        const i = e.payload;
        this.events.emit("operation.rejected", i);
      }
      e.type === a.BACK_BUTTON_PRESSED && (this.backButton.handlePressed(), this.events.emit("backButton"));
    });
  }
}
export {
  P as A,
  j as B,
  U as C,
  G as E,
  Se as H,
  h as I,
  L as O,
  V as P,
  ce as S,
  O as a,
  l as b,
  w as c,
  Le as d,
  u as e,
  f,
  y as g,
  m as h,
  M as i,
  z as j,
  g as k,
  se as l,
  d as m,
  oe as n,
  b as o,
  ne as p,
  a as q,
  _ as r,
  re as s,
  E as t,
  ae as u,
  S as v,
  ie as w,
  I as x,
  Ge as y,
  Z as z
};
