/* ============================================================================
   er 3D 视图引擎（three.js r161，ES module，按需加载）

   契约：与 2D 雷达共用同一份 /api/state 快照，不自己拉数据。
     create({canvas, labels, mapInfo, model}) -> R3D
     r3d.update(snapshot)     每 tick 喂快照（与 draw(s) 同源）
     r3d.setMap(mapInfo, url, onProgress, modelInfo) 换图（重新载模型）
     r3d.setCam(mode)         follow | orbit | top | fpv
     r3d.focus(key)           镜头对准某干员
     r3d.setPrefs(pref)       复用 2D 的开关（trail/ai/box/loot/mate/name/dist/cone）

   坐标：模型是绝对世界坐标（米），与游戏 UE 厘米的关系已在 build_m3d.py 验证：
       x = ue_x/100      y = ue_z/100(高度)      z = ue_y/100
   朝向：UE yaw 以 +X 为 0、向 +Y 增大；在本场景即绕 -Y 轴旋转 yaw。
   ========================================================================== */
import * as THREE from "three";

/* ------------------------------------------------------------------ 常量 */
const W2M = 0.01;                       // UE 厘米 -> 米
const C_SELF = 0x37e08a, C_MATE = 0x4c8dff, C_AI = 0xff9f43,
      C_BOX = 0xa97bff, C_UNK = 0xc9d1de;
const TEAM = [0xff5f6d, 0xff9f43, 0xffd54f, 0xc792ea, 0x4dd0e1, 0xf48fb1, 0xaed581, 0xff8a65];
const GRADE = { 1: 0xc9d1de, 2: 0x6bcf7f, 3: 0x4c8dff, 4: 0xc792ea, 5: 0xffb547, 6: 0xff5f6d };
const STATE = { down: 0xffcf4d, dying: 0xffcf4d, dead: 0xa97bff, revive: 0x37e08a };
// 3D 标签沿用 2D 的状态文案与等级配色
const LAB_ST = { down: "倒地", dying: "倒地", dead: "阵亡", revive: "救援", box_carried: "搬运中", box_searching: "搜刮中", box_empty: "已搜空", box_looted: "已搜刮" };
const LAB_GR = { 1: "#c9d1de", 2: "#6bcf7f", 3: "#4c8dff", 4: "#c792ea", 5: "#ffb547", 6: "#ff5f6d" };

const wpos = (w) => new THREE.Vector3(w[0] * W2M, w[2] * W2M, w[1] * W2M);

/* 第一视角相机：数据只有 ~20Hz（前端活动期 50ms 一轮询），渲染是 60fps。
   只用**临界阻尼平滑**（Unity SmoothDamp 同式），目标 = 最新采样，**不做预测/外推**：
     · 指数 lerp 只有位置状态没有速度，τ 小于一个数据间隔时会"到位即停"（台阶）；
       临界阻尼多带一个速度状态，任何 smoothTime 下都是"起步-滑行-减速"的滑动。
     · 曾试过按速度外推来补滞后，但提前量 = v×预测窗口（6m/s×60ms = 36cm）在停下时
       要收回来，观感就是"移过头再移回来"（用户反馈定位不准）→ 已去掉。
   现在的取舍：一定平滑、绝不冲过头，滞后 ≈ 平滑时间。滑块越大越顺滑、越小越跟手。 */
const FPV_TAU_MIN = 25;          // 位置平滑下限（ms）
const FPV_TAU_YAW_MIN = 18;      // 朝向平滑下限（ms）
const FPV_TAU_ROT_MAX = 0.06;    // 朝向平滑上限（秒）：转头是"瞄准"，60ms 以上就会被感知成延迟
const FPV_SNAP_DIST = 60;        // 距目标超过它（米）= 真瞬移/复活，直接对位
// 上行自报坐标（C2S ch3 loc）比下行槽位高一个常量 —— 实测中位 +86cm，脚底/上身口径差。
// 减掉它，上行那路就与下行同口径，眼高/吸附等逻辑无需分支。
const FPV_UP_Z_OFF = 0.86;
/* 第一视角插值（把相邻采样"连起来"，消掉 20Hz 台阶）。
   做法 = 标准实体插值：缓冲最近若干个采样，渲染时取 now-delay 时刻的插值位姿，
   于是相机在两次采样之间是**匀速直线**走过去的，而不是"追最新值再被下一个值打断"。
   代价是固定延迟 = delay，所以 delay 直接取「第一视角平滑」滑块（越大越顺滑、
   越小越跟手），用户已有这个旋钮，不再新增设置项。 */
const FPV_LERP_DELAY_MIN = 0.04;   // 插值延迟下限（秒）：再小经常取不到"下一个点"，就退回保持
const FPV_LERP_DELAY_MAX = 0.20;   // 上限：延迟换顺滑，最多到这
// 自动延迟的倍数（× 实测采样间隔中位）。一局真实对局实测（间隔中位 87ms、p90 141ms）:
//   1.0× → 延迟 87ms，但 50% 的间隔要"保持"（下一个点还没来）
//   1.5× → 131ms / 10.3%
//   2.0× → 174ms / 7.4%     ← 拿 43ms 额外滞后只换 3 个百分点，不划算
// 定 1.5×：把"必须保持"的比例压到约 1/10，同时不白送滞后。
const FPV_LERP_DELAY_K = 1.5;
const FPV_LERP_GAP_MAX = 0.5;      // 相邻采样间隔超它 = 数据断了，清缓冲重建（别跨空档插值）
// 位置阻尼的固定兜底值（秒）。插值负责顺滑，它只负责"别让一个坏点把镜头弹走"，
// 所以取很小；**不跟「第一视角平滑」滑块联动**——那个旋钮现在只决定插值延迟。
const FPV_DAMP_GUARD = 0.02;
/* 延迟随运动强度缩放（0~1 的比例 → 乘到 _fpvDelay 的结果上）。
   为什么：插值的代价是固定延迟，而它的**收益只在高速时**体现 —— 高速时相邻采样
   间距大、台阶明显，需要插值抹平；接近静止时采样本来就密（位置几乎不变），台阶
   不可见，插值只是白白带来延迟。恒定延迟的后果就是用户感觉到的：
     · 起步慢一拍（人已经动了，相机还在延迟窗口里）
     · 停止往前飘一段（数据停了，插值还把最后一段放完才停）
   所以按"运动强度"给延迟打 0~1 的系数：
     · 近乎静止 / 数据不再变化（服务器只在变化时发 → 说明人停了）→ 系数 0，直接跟
       最新采样，起步与停止都立即响应；
     · 正常行走 / 跑动 → 系数 1，满延迟 + 插值。
   系数本身做低通，且阈值附近不会来回跳（见下面的 STALE 判定与 _fpvK 平滑）。 */
const FPV_SAMPLE_MAX_JUMP = 5.0;   // 相邻采样间距超过它（米）= 不是真实移动（真人 300ms
                                   // 最多走 ~2m），是传送/换局/脏样本 → 清缓冲直接切过去，
                                   // 绝不让插值连起来（那会变成几十~上百 m/s 的横扫）。
const FPV_LERP_SPEED_FULL = 2.0;   // 达到这个速度（m/s）就给满延迟；走路约 3m/s
const FPV_LERP_K_TAU = 0.35;       // 系数的低通时间常数（秒）。不能太小：延迟变化等于平移
                                   // 插值时间轴 = 位置跳（实测 τ=0.08s 时单帧出现 40m/s 的
                                   // 速度尖峰，改到 0.35s 后消失）
const FPV_LERP_STALE_K = 2.0;      // 最新采样超过 2×采样间隔没更新 = 人停了 → 系数归 0
const FPV_MAX_SPEED = 160;       // 平滑中的最大速度（米/秒）：防脏值把相机甩飞
const FPV_MAX_TURN = 720;        // 朝向平滑最大角速度（度/秒）

/* 人物模型缩放（3D）：这是"整体再乘距离补偿"之前的基准值 ——
   远处补偿由 mk.g.scale 负责，这里只管基准大小。原来站姿是 1.0（自己 1.25）。 */
const CHAR_SCALE = 1.0;          // 队友/敌人/AI 的人物基准缩放（原 0.9，按用户要求放大一点）
const SELF_SCALE = 1.2;          // 自己（曾 1.25 → 1.12 → 1.2）
const DOWN_TILT = -Math.PI / 2;  // 倒地：绕 Z 放平（负号 = 头朝面朝方向）

/* 3D 画质档位（借鉴 Korr3D：限制渲染分辨率 + 低帧自动降档）。
   第一视角贴地平视要铺满整片地形，是**填充率**瓶颈——把 pixelRatio 从
   min(dpr,2) 压到 1.0 等于少画 4 倍像素，这是"第一视角卡、其它视角不卡"最
   直接的解法（轨道/俯视看的是缩小的整图，像素压力小得多）。
   档位只调渲染分辨率，不动几何/贴图，所以不会有"降档后地图变样"的问题。 */
const QUALITY_CAPS = { perf: 1.0, balanced: 1.35, high: 2.0 };
const QUALITY_STEPS = ["perf", "balanced", "high"];
const QUALITY_WARMUP_MS = 6000;   // 载入模型后的前几秒不计入帧率采样
const QUALITY_SAMPLE_MS = 4000;   // 采样窗口
const QUALITY_MIN_FPS = 32;       // 低于它就降一档（只降不升，与 Korr3D 一致）


/* 临界阻尼平滑（Unity SmoothDamp 同式）：返回 [新值, 新速度]。
   与指数 lerp 的本质区别是它带速度状态 —— 速度连续，因此任何 smoothTime 下
   都是"起步-滑行-减速停住"，不会出现"到位即停"的台阶/瞬移感。 */
function smoothDamp(cur, tgt, vel, smoothTime, dt, maxSpeed) {
  const st = Math.max(1e-4, smoothTime);
  const omega = 2 / st;
  const x = omega * dt;
  const exp = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
  const change = cur - tgt;
  const temp = (vel + omega * change) * dt;
  let nv = (vel - omega * temp) * exp;
  let out = tgt + (change + temp) * exp;
  if (maxSpeed > 0) {
    const cap = maxSpeed * dt;
    const d = out - cur;
    if (Math.abs(d) > cap) out = cur + (d > 0 ? cap : -cap);
    if (Math.abs(nv) > maxSpeed) nv = nv > 0 ? maxSpeed : -maxSpeed;
  }
  return [out, nv];
}

/* 角度版：走最短弧（度），避免 yaw 在 ±180 处绕一整圈 */
function smoothDampAngle(cur, tgt, vel, smoothTime, dt, maxSpeed) {
  let d = tgt - cur;
  while (d > 180) d -= 360;
  while (d < -180) d += 360;
  const r = smoothDamp(0, d, vel, smoothTime, dt, maxSpeed);
  return [cur + r[0], r[1]];
}

/* 三次 Hermite 插值：p0/p1 端点、m0/m1 端点速度（米/秒）、h 区间时长（秒）、u∈[0,1]。
   为什么不用两点线性：采样时间戳被客户端轮询节拍量化，每个区间的隐含速度各自偏一点，
   线性插值会把它原样变成"这段快、那段慢"。用邻居算端点速度后速度在区间之间连续。 */
function hermite3(out, p0, p1, m0, m1, h, u) {
  const u2 = u * u, u3 = u2 * u;
  const h00 = 2 * u3 - 3 * u2 + 1, h10 = u3 - 2 * u2 + u;
  const h01 = -2 * u3 + 3 * u2, h11 = u3 - u2;
  out.set(
    h00 * p0.x + h10 * h * m0.x + h01 * p1.x + h11 * h * m1.x,
    h00 * p0.y + h10 * h * m0.y + h01 * p1.y + h11 * h * m1.y,
    h00 * p0.z + h10 * h * m0.z + h01 * p1.z + h11 * h * m1.z);
}

const colorOf = (e) => {
  if (e.kind === "self") return C_SELF;
  if (e.kind === "mate") return C_MATE;
  if (e.kind === "box") return (e.is_ai === true || e.is_bot === true) ? C_BOX : 0xffb547;
  if (e.kind === "loot") return GRADE[e.grade] || 0xffffff;   // 未知物资 = 白色
  if (e.kind === "ai") return C_AI;
  return e.team > 0 ? TEAM[(e.team - 1) % TEAM.length] : C_UNK;
};

/* ------------------------------------------------------- 极简 GLB 读取器
   只认 build_m3d.py 产出的白模：单 buffer、每 mesh 一个 primitive、
   POSITION 为 int16（KHR_mesh_quantization，反量化在 node.matrix 里）、
   索引 uint16/uint32、无法线/UV/贴图。省掉整个 GLTFLoader 依赖。          */
function parseGLB(buf) {
  const dv = new DataView(buf);
  if (dv.getUint32(0, true) !== 0x46546c67) throw new Error("不是 GLB");
  let off = 12, js = null, bin = null;
  while (off + 8 <= dv.byteLength) {
    const len = dv.getUint32(off, true), type = dv.getUint32(off + 4, true);
    const body = off + 8;
    if (type === 0x4e4f534a) js = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, body, len)));
    else if (type === 0x004e4942) bin = { off: body, len: len };
    off = body + len;
  }
  if (!js || !bin) throw new Error("GLB 缺少 JSON/BIN 块");
  const CT = { 5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array,
               5125: Uint32Array, 5126: Float32Array };
  const NC = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 };
  const read = (ai) => {
    const a = js.accessors[ai], bv = js.bufferViews[a.bufferView];
    const Ctor = CT[a.componentType], n = a.count * NC[a.type];
    return new Ctor(buf, bin.off + (bv.byteOffset || 0) + (a.byteOffset || 0), n);
  };
  const out = [];
  for (const nd of js.nodes || []) {
    if (nd.mesh == null) continue;
    const prim = js.meshes[nd.mesh].primitives[0];
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(read(prim.attributes.POSITION), 3));
    g.setIndex(new THREE.BufferAttribute(read(prim.indices), 1));
    const m = new THREE.Matrix4();
    if (nd.matrix) m.fromArray(nd.matrix);
    out.push({ geometry: g, matrix: m, name: nd.name || "" });
  }
  return out;
}

/** 带进度的整包下载：模型 16~37MB，没有进度条的等待体验很糟 */
async function readAll(rsp, onProgress) {
  const total = Number(rsp.headers.get("Content-Length") || 0);
  if (!rsp.body || !onProgress) return rsp.arrayBuffer();
  const rd = rsp.body.getReader();
  const bufs = [];
  let got = 0;
  for (;;) {
    const { done, value } = await rd.read();
    if (done) break;
    bufs.push(value);
    got += value.length;
    // 压缩传输时 Content-Length 是线上字节，解压流会超出——进度夹到满格
    onProgress(Math.min(got, total), total);
  }
  const out = new Uint8Array(got);
  let o = 0;
  for (let i = 0; i < bufs.length; i++) {
    out.set(bufs[i], o); o += bufs[i].length;
    bufs[i] = null;   // 边拷边放:峰值内存≈1×模型,手机上少 50MB 就少一次标签被杀
  }
  return out.buffer;
}

/* ------------------------------------------------------------- 轨道控制
   自己实现（拖=环绕，滚轮/双指=推拉，右键/双指拖=平移），
   省一个 OrbitControls 依赖，也方便和「跟随」模式共存。                  */
class Orbit {
  constructor(dom) {
    this.dom = dom;
    this.target = new THREE.Vector3();
    this.dist = 260; this.yaw = Math.PI * 0.25; this.pitch = 0.85;
    this.userZoomed = false;   // 用户手动缩放过后，不再用预设距离覆盖视角远近
    // minDist 是"能放大到多近"的硬下限（米）。原来取 2 —— 一个 3m 高的火柴人
    // 站在 2m 外，再往前推就没反应了，看起来就是"放大到一定程度就卡住"。
    // 相机 near 是 0.5，所以下限只能压到 0.6 以上，再小近平面会把目标本身切掉。
    this.minDist = 0.6; this.maxDist = 4000;
    this.dirty = true;
    this.onUserPan = null;   // 平移回调：用于「拖动即脱离跟随」
    this._p = new Map(); this._last = null; this._pinch = 0;
    const opt = { passive: false };
    dom.addEventListener("pointerdown", (e) => this._down(e), opt);
    dom.addEventListener("pointermove", (e) => this._move(e), opt);
    dom.addEventListener("pointerup", (e) => this._up(e), opt);
    dom.addEventListener("pointercancel", (e) => this._up(e), opt);
    dom.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoom(Math.exp(e.deltaY * 0.0012));
    }, opt);
    dom.addEventListener("contextmenu", (e) => e.preventDefault());
  }
  zoom(f) { this.dist = Math.min(this.maxDist, Math.max(this.minDist, this.dist * f)); this.dirty = true; this.userZoomed = true; }
  _down(e) {
    this.dom.setPointerCapture?.(e.pointerId);
    this._p.set(e.pointerId, { x: e.clientX, y: e.clientY, btn: e.button });
    this._last = { x: e.clientX, y: e.clientY };
    if (this._p.size === 2) this._pinch = this._span();
  }
  _span() {
    const v = [...this._p.values()];
    return Math.hypot(v[0].x - v[1].x, v[0].y - v[1].y);
  }
  _move(e) {
    const p = this._p.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (this._p.size >= 2) {
      const s = this._span();
      if (this._pinch > 0 && s > 0) { this.zoom(this._pinch / s); this._pinch = s; }
      this.pan(dx * 0.5, dy * 0.5);
      return;
    }
    // 鼠标：左键拖动 = 旋转视角，右键（或 Shift+左键）= 平移视图；
    // 触摸：单指仍为旋转，双指缩放 + 平移
    const touch = e.pointerType === "touch";
    if ((!touch && p.btn === 2) || e.shiftKey) this.pan(dx, dy);
    else {
      this.yaw -= dx * 0.005;
      this.pitch = Math.min(1.55, Math.max(-1.35, this.pitch + dy * 0.005));
      this.dirty = true;
    }
  }
  _up(e) { this._p.delete(e.pointerId); if (this._p.size < 2) this._pinch = 0; }
  pan(dx, dy) {
    const k = this.dist * 0.0016;
    const s = Math.sin(this.yaw), c = Math.cos(this.yaw);
    // 屏幕「下」投影到地面是 sp·(s, 0, c)：斜视时同样 1px 对应更长的地面距离，
    // 不按俯角补偿，拖动就会和地面空间对不上（跟不上/斜着跑）。
    let sp = Math.sin(this.pitch);
    if (Math.abs(sp) < 0.25) sp = sp < 0 ? -0.25 : 0.25;
    const dyw = dy / sp;
    // 跟手：屏幕右 = (c, 0, -s)，屏幕下 = sp·(s, 0, c)
    this.target.x -= (c * dx + s * dyw) * k;
    this.target.z -= (-s * dx + c * dyw) * k;
    this.dirty = true;
    if (this.onUserPan) this.onUserPan();
  }
  apply(cam) {
    const cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    cam.position.set(
      this.target.x + this.dist * cp * Math.sin(this.yaw),
      this.target.y + this.dist * sp,
      this.target.z + this.dist * cp * Math.cos(this.yaw));
    cam.lookAt(this.target);
  }
}

/* ---------------------------------------------------------------- 标记池
   一个干员 = 立柱(高度线) + 地面环 + 朝向锥 + 悬浮片，全部 depthTest:false
   常驻最上层：雷达要的是「隔墙也看得见」，不是真实遮挡。                  */
function makeMarkerGeo() {
  const ring = new THREE.RingGeometry(0.85, 1.15, 24).rotateX(-Math.PI / 2);
  // 三角楔朝向锥:顶点须留在 +X——mesh 侧用 rotation.y = -yaw 承担全部朝向,
  // 这里再叠 rotateY 会拧出一个固定偏角(0902 实测 30°+z 压缩 ≈ -13.6°)。
  const cone = new THREE.CircleGeometry(3.6, 3, 0, Math.PI * 2)
    .rotateX(-Math.PI / 2);
  cone.scale(1, 1, 0.42);
  const stem = new THREE.CylinderGeometry(0.06, 0.06, 1, 6).translate(0, 0.5, 0);
  const chip = new THREE.CircleGeometry(1.05, 20);
  // 枪线：从中心沿 +X（与朝向锥同向）铺一条窄面；mesh 侧同样由 rotation.y 承担朝向
  const gun = new THREE.PlaneGeometry(16, 0.16).rotateX(-Math.PI / 2).translate(8, 0, 0);
  // 火柴人：头 + 躯干 + 双臂 + 双腿。整体朝 +X（与朝向锥/枪线同向）；
  // 四肢在水平面展开，俯视能看出人形，侧视也有体块感。
  // 线径按"千米级地图上仍能看见"取值（对齐地面环 0.3 的线宽）。
  const stickR = (r, len) => new THREE.CylinderGeometry(r, r, len, 6);
  // 分段：腿 1.3 / 躯干 1.05 / 头 0.64，总高约 3.0，三段首尾相接不重叠
  const LEG = 1.3, TORSO = 1.05, HEAD = 0.64, ARM = 1.05;
  const yShoulder = LEG + TORSO - 0.14;   // 肩部：躯干顶部略下，确保在头之下
  const stick = {
    head: new THREE.SphereGeometry(HEAD / 2, 10, 8).translate(0, LEG + TORSO + HEAD / 2, 0),
    torso: stickR(0.15, TORSO).translate(0, LEG + TORSO / 2, 0),
    // 双臂自肩部向左右（±Z）自然张开并略下垂。朝向由整体 rotation.y 随 yaw 一起转，
    // 所以这里的形状只需表达「人形」，前后方向交给旋转。
    armL: stickR(0.12, ARM).rotateX(Math.PI / 2 + 0.35)
      .translate(0, yShoulder - 0.18, ARM / 2 - 0.06),
    armR: stickR(0.12, ARM).rotateX(-(Math.PI / 2 + 0.35))
      .translate(0, yShoulder - 0.18, -(ARM / 2 - 0.06)),
    legL: stickR(0.14, LEG).translate(0, LEG / 2, 0.19),
    legR: stickR(0.14, LEG).translate(0, LEG / 2, -0.19),
  };
  // 人物方框：可选外框（「人物方框」开关）。EdgesGeometry 只留 12 条棱，一个
  // LineSegments 就是 1 个 draw call —— 比再搭 6 个面片便宜，且任何角度都看得见。
  // 尺寸取火柴人包络（高 3.0 / 宽 1.35 / 厚 1.15），几何抬到以脚为原点的中心，
  // 于是它能跟着 fig 组一起转身 / 倒地 / 缩放，不必每帧重算。
  const bbox = new THREE.EdgesGeometry(
    new THREE.BoxGeometry(1.15, 3.0, 1.35).translate(0, 1.5, 0));
  return { ring, cone, stem, chip, gun, stick, bbox };
}

class Marker {
  constructor(geo, mats) {
    this.g = new THREE.Group();
    this.ring = new THREE.Mesh(geo.ring, mats.ring);
    this.cone = new THREE.Mesh(geo.cone, mats.cone);
    this.stem = new THREE.Mesh(geo.stem, mats.stem);
    this.chip = new THREE.Mesh(geo.chip, mats.chip);
    this.gun = new THREE.Mesh(geo.gun, mats.gun);
    for (const m of [this.ring, this.cone, this.stem, this.chip, this.gun]) {
      m.renderOrder = 20; m.frustumCulled = false; this.g.add(m);
    }
    // 火柴人取代原来的悬浮圆片：6 个部件共用几何体，逐实例建 Mesh。
    // 装进一个 fig 组：转身/倒地/缩放只写在组上（原来逐部件写 6 次矩阵），
    // 且"倒地"= 整具身体绕脚点刚性旋转 —— 各部件几何都烘在同一原点，绕同一轴
    // 同一角度就是整体旋转，不用另算偏移。rotation.order 用 YXZ：先绕 Z 放平，
    // 再绕 Y 转身，于是倒下的方向自然跟着面朝方向。
    this.fig = new THREE.Group();
    this.fig.rotation.order = "YXZ";
    this.g.add(this.fig);
    this.stick = ["head", "torso", "armL", "armR", "legL", "legR"].map((k) => {
      const m = new THREE.Mesh(geo.stick[k], mats.stick);
      m.renderOrder = 20; m.frustumCulled = false; this.fig.add(m);
      return m;
    });
    this.stem.visible = false;
    this.gun.visible = false;
    this.chip.visible = false;   // 悬浮圆片由火柴人取代
    // 人物方框：进 fig 组才能跟着转身/倒地/缩放一起走。开关只切 visible，
    // 不删不建（删了重建会让开关有一瞬间的空档）。
    this.box = new THREE.LineSegments(geo.bbox, mats.bbox);
    this.box.renderOrder = 40; this.box.frustumCulled = false; this.box.visible = false;
    this.fig.add(this.box);
    this.h = 2.2;
  }
  setColor(c) {
    if (this._c === c) return;
    this._c = c;
    for (const m of [this.ring, this.cone, this.stem, this.chip, this.gun, this.box, ...this.stick]) {
      m.material = m.material.clone(); m.material.color.setHex(c);
    }
  }
}

/* ================================================================== 引擎 */
class R3D {
  constructor(o) {
    this.canvas = o.canvas;
    this.labels = o.labels;
    this.warnSvg = o.warn || null;   // 屏幕外预警的 SVG 层（页面提供，可缺省）
    this.mapInfo = o.mapInfo || null;
    this.pref = o.pref || {};
    this.followKey = "__self";     // 跟随目标：__self 或某个实体 key
    this.bagLv = o.bagLv || null;   // 背包名 -> 等级 / 容量（由页面传入）
    this.bagCap = o.bagCap || null;
    this.onPick = o.onPick || null;
    this.camMode = "follow";
    this.sel = null;
    this.selfPos = new THREE.Vector3();
    // 第一视角相机状态：数据只有 ~20Hz 而渲染是 60fps，靠临界阻尼平滑 + 目标预测
    this._fpvPos = new THREE.Vector3();
    this._fpvYaw = null;
    this._fpvPitch = null;      // 自机俯仰（度，正=抬头）；只有上行 C2S 解得出
    this._fpvT = 0;
    this._fpvInited = false;
    this._fpvVel = new THREE.Vector3();     // 位置平滑的当前速度（米/秒）
    this._fpvVelYaw = 0;                    // 朝向平滑的当前角速度（度/秒）
    this._fpvVelPitch = 0;
    this._fpvPitchPrev = null;
    this._tmpV = new THREE.Vector3();       // 复用临时向量，避免每帧分配
    this._fpvUpVec = new THREE.Vector3();   // 上行坐标的落点（复用，避免每帧分配）
    this._fpvBuf = [];                      // [{t, p, yaw, pitch}] 采样缓冲（秒 + 米）
    this._fpvSmp = { p: new THREE.Vector3(), yaw: null, pitch: null };
    this._vm0 = new THREE.Vector3(); this._vm1 = new THREE.Vector3();   // Hermite 端点速度
    this._fpvK = null;         // 运动强度系数（延迟缩放），带低通
    this._fpvGap = null;       // 采样间隔 EMA（只在推入采样时更新）
    this._fpvLastPushT = null; // 最近一次推入的客户端时刻（判人停了用）
    this._fpvCut = false;      // 数据断过 → 下一帧硬切（不要滑）
    this._fpvDt = 1 / 60;      // 上一帧 dt（_fpvMotionK 的低通用）
    this._fpvSrvOff = null;    // 服务端时钟 -> 客户端时钟的偏移（最小观测 delay 锚）
    this._fpvLastSrv = null;   // 上一个服务端采样时刻（判回退/换局）
    this.hasSelf = false;
    this.selfOnMap = false;
    this.modelInfo = o.model || null;
    this.modelBounds = null;
    this.modelCenter = new THREE.Vector3();
    this.modelGroundY = 0;
    this._mk = new Map();
    this._lbl = new Map();
    this._lblD = new Map();       // 距离标注：独立挂在人物脚下（不在头顶信息行里）
    this._trail = new Map();
    this._raf = 0;
    this._stat = { tris: 0, batches: 0, loading: false, err: "" };

    // 画质相关：性能档在创建时关掉 MSAA（抗锯齿是填充率大头；WebGL 上下文一旦建好
    // 就不能再改 antialias，所以它读的是"进 3D 那一刻"的档位）；stencil 我们不用。
    const q0 = (o.pref && o.pref.q3d) || "auto";
    const r = new THREE.WebGLRenderer({ canvas: o.canvas, antialias: q0 !== "perf",
                                        alpha: false, stencil: false,
                                        powerPreference: "high-performance" });
    // 渲染分辨率由 setQuality() 统一设定（见 QUALITY_CAPS），这里不再写死
    r.setClearColor(0x05070c, 1);
    this.renderer = r;
    this.onToast = o.onToast || null;   // 画质自动降档时给页面提示（可空）
    // 注意：setQuality 会调 resize()，必须等 camera/renderer/尺寸全部就绪后再调 ——
    // 构造末尾（this._loop() 之前）统一调用，别挪到这里，否则构造抛异常 = 3D 起不来。

    const s = new THREE.Scene();
    s.fog = new THREE.Fog(0x05070c, 300, 1800);
    this.scene = s;

    s.add(new THREE.HemisphereLight(0xa8c4ff, 0x121820, 2.1));
    const d = new THREE.DirectionalLight(0xffffff, 1.35);
    d.position.set(0.6, 1, 0.35);
    s.add(d);

    this.camera = new THREE.PerspectiveCamera(58, 1, 0.5, 12000);
    this.orbit = new Orbit(o.canvas);
    this.orbit.onUserPan = () => this._userPan();

    this.world = new THREE.Group();      // 底图
    this.ents = new THREE.Group();       // 干员标记
    s.add(this.world, this.ents);

    this.grid = new THREE.GridHelper(2000, 100, 0x1c3352, 0x121d30);
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.42;
    s.add(this.grid);

    this.geo = makeMarkerGeo();
    const base = (op, side) => new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: op, depthTest: false,
      depthWrite: false, side: side || THREE.DoubleSide, fog: false });
    this.mats = { ring: base(0.95), cone: base(0.34), stem: base(0.5), chip: base(0.92), gun: base(0.9),
                  stick: base(0.95),
                  // 方框是线框，用 LineBasicMaterial；其余参数与 mesh 素材一致
                  bbox: new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true,
                    opacity: 0.95, depthTest: false, depthWrite: false, fog: false }) };

    this.modelMat = new THREE.MeshLambertMaterial({
      color: 0xb9c3d2, flatShading: true, side: THREE.DoubleSide });

    this._ray = new THREE.Raycaster();
    o.canvas.addEventListener("click", (e) => this._pick(e));
    // 画质：必须放在构造末尾（camera/renderer 都已就绪）
    this.setQuality(this.pref && this.pref.q3d ? this.pref.q3d : "auto");
    this.resize();
    this._loop();
  }

  /* ------------------------------------------------------------ 底图加载 */
  async setMap(mapInfo, url, onProgress, modelInfo) {
    // 切图可能被后台流轮转与手点同时触发:记最新目标,由唯一载入循环串行接管,
    // 避免两次载入并发互拆(world 互清)后 _url 已改而模型没换。
    this._pendingMap = { mapInfo: mapInfo || this.mapInfo, url, onProgress,
                         modelInfo: modelInfo || null };
    if (this._loadingMap) return this._loadingMap;
    this._stat.loading = true;
    this._loadingMap = (async () => {
      while (this._pendingMap) {
        const job = this._pendingMap;
        this._pendingMap = null;
        const u = job.url;
        if (this._url === u) continue;
        this.mapInfo = job.mapInfo;
        this.modelInfo = job.modelInfo;
        this._stat.err = "";
        this._stat.tris = 0;
        this._stat.batches = 0;
        while (this.world.children.length) {
          const c = this.world.children.pop();
          c.geometry?.dispose();
        }
        try {
          const rsp = await fetch(u, { cache: "force-cache" });
          if (!rsp.ok) throw new Error("HTTP " + rsp.status);
          const parts = parseGLB(await readAll(rsp, job.onProgress));
          let tris = 0;
          for (const p of parts) {
            const m = new THREE.Mesh(p.geometry, this.modelMat);
            // Quantized terrain slabs can have a zero axis scale. Applying the
            // matrix through Object3D.applyMatrix4() decomposes it first;
            // decomposition divides by that zero scale and poisons the mesh
            // transform with NaN. Keep the authored matrix intact instead.
            m.matrixAutoUpdate = false;
            m.matrix.copy(p.matrix);
            m.matrixWorldNeedsUpdate = true;
            this.world.add(m);
            tris += p.geometry.index.count / 3;
          }
          this._stat.tris = tris; this._stat.batches = parts.length;
          const box = new THREE.Box3().setFromObject(this.world);
          if (box.isEmpty() || ![box.min.x, box.min.y, box.min.z,
                                box.max.x, box.max.y, box.max.z].every(Number.isFinite)) {
            throw new Error("模型包围盒无效");
          }
          const c = box.getCenter(new THREE.Vector3());
          const size = box.getSize(new THREE.Vector3());
          const ground = Number(this.modelInfo?.ground?.poi_med);
          const groundY = Number.isFinite(ground) ? ground : c.y;
          this.modelBounds = box.clone();
          this.modelGroundY = groundY;
          this.modelCenter.set(c.x, groundY, c.z);
          this.grid.position.set(c.x, groundY - 0.5, c.z);
          this.grid.scale.setScalar(Math.max(1, Math.max(size.x, size.z) / 2000));
          const span = Math.max(50, size.x, size.z);
          this.orbit.maxDist = Math.max(4000, span * 4);
          this.selfOnMap = this.hasSelf && this._insideModel(this.selfPos);
          if (this.camMode === "follow" && this.selfOnMap) {
            this.orbit.target.copy(this.selfPos);
            if (!this.orbit.userZoomed) this.orbit.dist = Math.min(this.orbit.dist, 160);
          } else {
            // 不用完整 bbox 的 Y 中心：htjd 含数公里深的离群几何，
            // 用它初始化会把相机放到地下，看起来就像模型没有加载。
            this.orbit.target.copy(this.modelCenter);
            // 用户手动调过缩放就保留：重进 3D / 换地图不再重置视角远近
            if (!this.orbit.userZoomed) this.orbit.dist = Math.max(80, span * 0.75);
          }
          this.camera.far = Math.max(4000, size.length() * 3, span * 4);
          this._farMax = this.camera.far;   // 第一视角会把远平面临时收小，这里存全图用值
          this.camera.updateProjectionMatrix();
          this._url = u;
        } catch (err) {
          this._stat.err = String(err && err.message || err);
        }
      }
    })();
    try {
      return await this._loadingMap;
    } finally {
      this._loadingMap = null;
      this._stat.loading = false;
    }
  }

  _insideModel(p) {
    const b = this.modelBounds;
    if (!b || b.isEmpty()) return false;
    const span = Math.max(b.max.x - b.min.x, b.max.z - b.min.z);
    const margin = Math.max(25, span * 0.08);
    return p.x >= b.min.x - margin && p.x <= b.max.x + margin
      && p.z >= b.min.z - margin && p.z <= b.max.z + margin;
  }

  /* ---------------------------------------------------------------- 数据 */
  setPrefs(p) { this.pref = p || {}; }
  setCam(mode) {
    this.camMode = mode;
    this._applyFpvHide();                          // 切换视角立即生效，不等下次轮询
    if (mode === "fpv") this._fpvInited = false;   // 重新进入时直接对位，别从旧位置飞过去
    if (mode === "top") { this.orbit.pitch = 1.5; this.orbit.dirty = true; }
    // 各模式只决定「相机盯谁」和俯仰角；缩放远近始终保留用户当前的手动设置
    if (mode === "follow" && this.hasSelf && this.selfOnMap) {
      this.orbit.target.copy(this.selfPos);
    } else if (mode === "follow" && this.modelBounds) {
      this.orbit.target.copy(this.modelCenter);
    }
    if (mode === "orbit" && this.orbit.pitch > 1.4) this.orbit.pitch = 0.85;
    if (mode === "chase") {
      this.orbit.pitch = 0.72;
      if (this.hasSelf && this.selfOnMap) this.orbit.target.copy(this.selfPos);
      this.orbit.dirty = true;
    }
  }
  // 第一视角：相机就贴在"跟随目标"头上，不渲染它的火柴人/圆片，否则糊住视野
  _applyFpvHide() {
    const hide = this.camMode === "fpv" ? (this.followKey || "__self") : null;
    for (const [k, mk] of this._mk) mk.g.visible = (k !== hide);
  }

  // 用户主动平移 = 想自由看：立刻退出跟随，否则跟随会把相机拽回去。
  // 俯视（top）同样在"每帧钉回跟随目标"的名单里（见 update 末尾那段），
  // 所以它也必须一起退出，否则右键拖完一松手就被拽回去，看着像拖动失灵。
  // 直接改 camMode 而不走 setCam("orbit")：后者会把超过 1.4 的俯角压回 0.85，
  // 于是"俯视里平移"会把镜头从正俯视掀成斜视 —— 用户要的是平移，不是换视角。
  _userPan() {
    if (this.camMode === "orbit" || this.camMode === "fpv") return;
    this.camMode = "orbit";
    if (typeof this.onCam === "function") this.onCam("orbit");
  }
  focus(key, snap) {
    this.sel = key;
    const e = (snap || this._snap || {}).entities?.find((x) => x.key === key);
    if (e && e.world) {
      this.orbit.target.copy(wpos(e.world));
      if (this.camMode === "follow") this.camMode = "orbit";
      if (!this.orbit.userZoomed && this.orbit.dist > 300) this.orbit.dist = 150;
      this.orbit.dirty = true;
    }
  }

  update(s) {
    this._snap = s;
    // 采样点在这里压（= 收到这一份响应的时刻），**不要放在渲染循环里**：
    // 放循环里会被帧边界量化（60fps → 16.7ms 粒度），而数据间隔才 42ms，
    // 时间戳 ±16.7ms 的抖动会直接变成"这段快那段慢"的微抖（实测过）。
    if (this.camMode === "fpv") {
      const p = this._fpvPosSource();
      if (p) {
        const sp = s.self_pitch;
        this._fpvPush(p, this._fpvYawSource(),
                      (typeof sp === "number" && isFinite(sp)) ? sp : null,
                      s.self_up_ts);
      }
    }
    const P = this.pref;
    const live = [];
    const wasSelfOnMap = this.selfOnMap;
    this.hasSelf = !!s.self;
    if (s.self) {
      this.selfPos.copy(wpos(s.self));
      this.selfOnMap = this._insideModel(this.selfPos);
      live.push({ key: "__self", kind: "self", world: s.self, yaw: s.self_yaw,
                  name: s.self_name || "自己", hp: s.self_hp });
    } else this.selfOnMap = false;
    for (const e of s.entities || []) {
      if (!e.world) continue;
      if (e.kind === "box" && !P.box) continue;
      if (e.kind === "ai" && !P.ai) continue;
      if (e.kind === "loot" && !P.loot) continue;
      if (e.kind === "mate" && !P.mate) continue;
      live.push(e);
    }

    const seen = new Set();
    for (const e of live) {
      seen.add(e.key);
      let mk = this._mk.get(e.key);
      if (!mk) {
        mk = new Marker(this.geo, this.mats);
        this.ents.add(mk.g);
        this._mk.set(e.key, mk);
      }
      const p = wpos(e.world);
      const col = e.status_key && STATE[e.status_key] ? STATE[e.status_key] : colorOf(e);
      mk.setColor(col);
      mk.g.position.copy(p);
      const loot = e.kind === "loot" || e.kind === "box";
      const h = loot ? 1.1 : (e.kind === "self" ? 3.9 : 3.1);
      mk.chip.position.y = h;
      mk.stem.scale.y = h;
      mk.stem.visible = true;
      mk.ring.visible = !loot;
      const showCone = P.cone !== 0 && e.yaw != null && !e.dead && !loot;
      mk.cone.visible = showCone;
      mk.gun.visible = showCone;
      if (e.yaw != null) {
        const ry = -e.yaw * Math.PI / 180;
        if (showCone) {
          mk.cone.rotation.y = ry;
          mk.gun.rotation.y = ry;
          // 枪线长度跟随 2D 滑块（几何基准 16 对应 92px）
          mk.gun.scale.x = Math.max(0.1, (Number(P.gunlen) || 92) / 92);
        }
        // 火柴人独立跟随 yaw 转身：即使隐藏朝向锥，人形也要面朝正确方向
        // （写在 fig 组上：1 次矩阵更新顶原来的 6 次）
        if (mk.fig.rotation.y !== ry) mk.fig.rotation.y = ry;
      }
      // 倒地 → 躺下（原来一律站着）。俯仰仍由上面的 yaw 决定，所以倒下的方向
      // 就是它面朝的方向；绕脚点旋转，人贴在地面上。
      const downed = e.status_key === "down" || e.status === "倒地";
      const tilt = downed ? DOWN_TILT : 0;
      if (mk.fig.rotation.z !== tilt) mk.fig.rotation.z = tilt;
      // 人物大小：设置里的「人物大小」百分比（50~200%，默认 100）乘在基准值上
      const sizeMul = Math.max(0.3, Math.min(3, (Number(this.pref.charsize) || 100) / 100));
      // 图层优先级：**人物最高**（自己 > 其它人物 > 物资/盒子）。
      // 所有标记材质都是 depthTest:false（雷达式叠加层、隔墙可见），这类透明叠加
      // 不按真实前后遮挡画，而是按"物体中心到相机距离"排序 —— 谁高谁离相机近，
      // 结果随机。所以这里用 renderOrder 显式定死优先级：人物永远压在物资/盒子之上。
      // renderOrder 只在变化时写（traverse 有成本）。
      const ro = (e.kind === "self") ? 50 : (loot ? 20 : 40);
      if (mk._ro !== ro) {
        mk._ro = ro;
        // isLine 也要一起改：方框是 LineSegments，不是 Mesh，漏掉它就会按默认
        // 距离排序去和人物抢前后，出现"框在人身前/身后闪烁"。
        mk.g.traverse((o) => { if (o.isMesh || o.isLine) o.renderOrder = ro; });
      }
      const sc = loot ? 0.62 : (e.kind === "self" ? SELF_SCALE : CHAR_SCALE) * sizeMul;
      if (mk._sc !== sc) {                    // 只在基准缩放变化时写（省矩阵更新）
        mk.ring.scale.setScalar(sc);
        mk.chip.scale.setScalar(sc);
        mk.fig.scale.setScalar(sc);
        mk._sc = sc;
      }
      // 火柴人只给人物（自己/队友/士兵/AI）；物资与死亡盒保持原始标记形态
      mk.fig.visible = !loot;
      // 人物方框：给人物模型套一个线框（视图栏「人物方框」开关）。它挂在 fig 组里，
      // 所以物资/死亡盒那边随 fig.visible=false 一起消失，颜色也跟随人物状态色。
      if (mk.box.visible !== (!loot && !!P.box3d)) mk.box.visible = !loot && !!P.box3d;
      // 第一视角：隐藏相机所在的那个目标（自己 / 正在被跟随的实体）
      mk.g.visible = !(this.camMode === "fpv" && e.key === (this.followKey || "__self"));
      mk._e = e;
      this._label(e, p, h);
    }
    for (const [k, mk] of this._mk) {
      if (seen.has(k)) continue;
      this.ents.remove(mk.g);
      this._mk.delete(k);
      const l = this._lbl.get(k);
      if (l) { l.remove(); this._lbl.delete(k); }
      const ld = this._lblD.get(k);
      if (ld) { ld.remove(); this._lblD.delete(k); }
    }
    if (this.camMode !== "orbit") {
      const tg = this._followTarget();
      if (tg) {
        if (this.camMode === "follow" && wasSelfOnMap) this.orbit.target.lerp(tg, 0.25);
        else this.orbit.target.copy(tg);          // 跟随/俯视/朝向：贴合跟随目标（自己或别人）
      }
    }
  }

  _label(e, p, h) {
    if (!this.labels) return;
    const P = this.pref;
    let d = null;
    if (e.rel) {                                   // 距离异常大 = 自机坐标未解出，宁可不显示
      const dx = e.rel[0], dy = e.rel[1];
      if (Number.isFinite(dx) && Number.isFinite(dy)) {
        const dd = Math.hypot(dx, dy) / 100;
        if (dd <= 5000) d = dd;
      }
    }
    // 距离标注：单独一个元素挂在人物【脚下】（不是头顶信息行里的一段）。头顶
    // 那行留给状态/编号/武器/名字，距离自己贴地显示，跟人物位置一一对应。
    // 独立生命周期：只开「距离」开关、其余全关时，也要能单独看见距离。
    const farLoot = e.kind === "loot" && d != null && d > 120;   // 远处物资不标注
    const showD = P.dist !== 0 && d != null && !farLoot;
    let dl = this._lblD.get(e.key);
    if (!showD) {
      if (dl) { dl.remove(); this._lblD.delete(e.key); }
    } else {
      if (!dl) {
        dl = document.createElement("div");
        dl.className = "r3-dst";
        dl.style.zIndex = (e.kind === "self") ? "40" : (e.kind === "loot" || e.kind === "box") ? "20" : "30";
        this.labels.appendChild(dl);
        this._lblD.set(e.key, dl);
      }
      const txt = d.toFixed(0) + "m";
      if (dl._t !== txt) { dl.textContent = txt; dl._t = txt; }
      dl._p = new THREE.Vector3(p.x, p.y + 0.05, p.z);   // 模型底 = 脚下
      dl._k = e.key;
      dl._top = 1;                    // 顶边贴锚点 -> 画在人物下方
      dl._c = colorOf(e);
    }
    const l1 = [];
    if (e.status_key && LAB_ST[e.status_key] && e.kind !== "mate")
      l1.push("<b>" + LAB_ST[e.status_key] + "</b>");   // 队友不显示状态（同 2D）
    if (e.team > 0 && e.kind !== "mate") l1.push("T" + e.team);
    if (P.wpn !== 0) {
      const w = (e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon));
      if (w) l1.push(String(w));
    }
    if (P.name !== 0) { const n = e.hero || e.name; if (n) l1.push(String(n)); }
    const l2 = [];
    if (P.gear !== 0 && e.kind !== "loot" && e.kind !== "box") {
      const GC = (lv) => LAB_GR[Number(lv)] || "#c9d1de";
      const DN = (v) => (Array.isArray(v) && v[0] != null) ? String(Math.round(v[0])) : "?";
      if (e.helmet) l2.push('<span style="color:' + GC(e.helmet) + '">头' + DN(e.helmet_dur) + "</span>");
      if (e.vest) l2.push('<span style="color:' + GC(e.vest) + '">甲' + DN(e.vest_dur) + "</span>");
      const blv = (e.bp && this.bagLv) ? this.bagLv[e.bp] : null;
      if (blv) {
        const bc = (e.bp && this.bagCap) ? this.bagCap[e.bp] : null;
        l2.push('<span style="color:' + GC(blv) + '">包' + (bc != null ? bc : blv) + "</span>");
      }
    }
    let hpf = null;
    if (P.hp !== 0 && e.hp && e.hp.total && e.hp.total[1] > 0 && !e.dead) {
      hpf = Math.max(0, Math.min(1, e.hp.total[0] / e.hp.total[1]));
    }
    // 敌人标识：仅敌队玩家，压在状态行上方（同 2D），由「敌人标识」开关独立控制
    // 倒地（down/dying）时标识换成橙色「倒地」，与活敌的红色「敌人」区分
    const foe = P.foe !== 0 && e.kind === "player" && e.team > 0;
    const foeDown = foe && !e.dead && (e.status_key === "down" || e.status_key === "dying");
    // 状态行/装备行/血条装进 .bd：底色画在它身上（对齐 2D 的 .tag），
    // 「敌人」红标留在 .bd 之外，于是红标浮在底色框上方而不是被框进黑底。
    const rows = (l1.length ? '<div class="l1">' + l1.join(" ") + "</div>" : "")
      + (l2.length ? '<div class="l2">' + l2.join("") + "</div>" : "")
      + (hpf != null ? '<div class="hp"><i style="width:' + (hpf * 100).toFixed(0) + "%;background:"
          + (hpf > .66 ? "#37e08a" : hpf > .33 ? "#ffcf4d" : "#ff5f6d") + '"></i></div>' : "");
    const html = (foe ? '<div class="foe' + (foeDown ? " down" : "") + '">'
        + (foeDown ? "倒地" : "敌人") + "</div>" : "")
      + (rows ? '<div class="bd">' + rows + "</div>" : "");
    let el = this._lbl.get(e.key);
    if (!html || farLoot) {
      if (el) { el.remove(); this._lbl.delete(e.key); }
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.className = "r3-lbl";
      el.style.zIndex = (e.kind === "self") ? "40" : (e.kind === "loot" || e.kind === "box") ? "20" : "30";
      this.labels.appendChild(el);
      this._lbl.set(e.key, el);
    }
    if (el._t !== html) { el.innerHTML = html; el._t = html; }
    // 敌队玩家标记：给「敌人连线」用。独立于「敌人标识」开关（P.foe）——
    // 关掉敌人标识只该去掉那个红底徽标，不该把连线一起带走。
    const isFoe = e.kind === "player" && e.team > 0;
    const foeTag = isFoe ? "1" : "";
    if (el.dataset.foe !== foeTag) el.dataset.foe = foeTag;
    el._p = new THREE.Vector3(p.x, p.y + h + 1.1, p.z);   // 头顶：底边贴锚点
    el._k = e.key;
    el._c = colorOf(e);
  }

  /* ---------------------------------------------------------------- 渲染 */
  resize() {
    if (!this.renderer || !this.camera || !this.canvas) return;   // 构造期防御
    const w = this.canvas.clientWidth || 1, h = this.canvas.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  /* 3D 画质：'auto' 或 perf/balanced/high。cap = 渲染分辨率上限（pixelRatio）。
     auto 先按设备粗判一档，之后由 _autoDowngrade 按实测帧率往下调（只降不升）。 */
  setQuality(id) {
    this._q = QUALITY_CAPS[id] ? id : "auto";
    if (this._q === "auto") {
      // 触屏 / 低核数 / 低内存 -> 直接给低档；否则 balanced（多数机器够用）
      const cores = navigator.hardwareConcurrency || 4;
      const mem = navigator.deviceMemory || 8;
      const touch = matchMedia("(pointer: coarse)").matches;
      this._cap = (touch || cores <= 4 || mem <= 4) ? QUALITY_CAPS.perf
                                                    : QUALITY_CAPS.balanced;
      this._warmUntil = performance.now() + QUALITY_WARMUP_MS;
      this._qFrames = 0;
      this._qStart = performance.now();
    } else {
      this._cap = QUALITY_CAPS[this._q];
    }
    this._applyQuality();
  }

  _applyQuality() {
    if (!this.renderer) return;                                   // 构造期防御
    const dpr = Math.max(0.75, Number(devicePixelRatio || 1));
    this.renderer.setPixelRatio(Math.min(dpr, this._cap));
    this.resize();               // setPixelRatio 要配一次 setSize 才生效
  }

  /* 实测帧率低于阈值就降一档（分辨率减半级）。只降不升：升档会在低端机上
     来回抖动（Korr3D 也是这个策略）。 */
  _autoDowngrade() {
    if (this._q !== "auto") return;
    const now = performance.now();
    if (now < (this._warmUntil || 0)) return;
    this._qFrames = (this._qFrames || 0) + 1;
    const elapsed = now - this._qStart;
    if (elapsed < QUALITY_SAMPLE_MS) return;
    const fps = this._qFrames * 1000 / elapsed;
    this._qFrames = 0;
    this._qStart = now;
    if (fps >= QUALITY_MIN_FPS) return;
    const idx = QUALITY_STEPS.findIndex((k) => QUALITY_CAPS[k] === this._cap);
    if (idx <= 0) return;                       // 已经是最低档
    this._cap = QUALITY_CAPS[QUALITY_STEPS[idx - 1]];
    this._applyQuality();
    if (this.onToast) {
      this.onToast("3D 帧率偏低（" + Math.round(fps) + " FPS），画质已自动降到"
        + (this._cap <= 1.0 ? "流畅" : "平衡"));
    }
  }

  _pick(ev) {
    if (!this.onPick) return;
    const r = this.canvas.getBoundingClientRect();
    const nd = new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1,
                                -((ev.clientY - r.top) / r.height) * 2 + 1);
    this._ray.setFromCamera(nd, this.camera);
    const hits = this._ray.intersectObjects(this.ents.children, true);
    if (!hits.length) return;
    let g = hits[0].object;
    while (g && !this._mkOf(g)) g = g.parent;
    const mk = g && this._mkOf(g);
    if (mk && mk._e) this.onPick(mk._e);
  }
  _mkOf(g) { for (const m of this._mk.values()) if (m.g === g) return m; return null; }
  setFollowKey(k) { this.followKey = k || "__self"; }
  _followTarget() {
    const k = this.followKey || "__self";
    if (k === "__self") return (this.hasSelf && this.selfOnMap) ? this.selfPos : null;
    const mk = this._mk.get(k);
    return mk ? mk.g.position : (this.hasSelf && this.selfOnMap ? this.selfPos : null);
  }

  /* 第一视角的相机目标点。自己时优先用**上行**自报坐标（C2S ch3，15Hz），
     拿不到才退回下行槽位（有效只有 4~6.5Hz）。

     为什么值得换：第一视角要的是"平移不要顿"。移动时下行槽位每步跳
     0.20m 中位 / 0.63m p90（间隔 p90 198ms、p99 1.6s），而俯仰旋转每步只有
     0.3°/42ms —— 同一个屏幕上，平移粗 10~100 倍，这才是"横视不顺、俯仰很顺"
     的真正原因（旋转和俯仰用的是同一套平滑参数，问题不在平滑）。

     上行 loc 与下行槽位实测：水平差 **中位 0.0cm**（可直接顶替），
     Z 差一个恒定的 **+86cm**（脚底 / 上身口径差）—— 所以这里减掉它，
     让返回值仍是"脚底"口径，上层 `_fpvPos.y + 眼高` 的算法一行都不用改。

     **只在第一视角用**：第三人称下人物模型本身仍由下行驱动，相机若单独用上行，
     模型会相对相机抖 —— 那比现在更难看。FPV 里自机模型是隐藏的，没有这个问题。 */
  _fpvPosSource() {
    const k = this.followKey || "__self";
    if (k === "__self") {
      const up = this._snap && this._snap.self_up_loc;
      if (Array.isArray(up) && up.length >= 3) {
        const a = Number(up[0]), b = Number(up[1]), c = Number(up[2]);
        if (Number.isFinite(a) && Number.isFinite(b) && Number.isFinite(c)) {
          return this._fpvUpVec.set(a * W2M, c * W2M - FPV_UP_Z_OFF, b * W2M);
        }
      }
    }
    return this._followTarget();
  }

  /* 第一视角朝向来源：**优先上行瞄向**（self_aim_yaw），拿不到退回下行身体朝向。
     曾用过下行身体朝向并留下"已退回"的注释，后来上行位姿从 7.4Hz 提到 15.0Hz
     （c2s_self.parse_fgp 收下 lite 尾型）后又切回瞄向 —— 第一视角的"我在看哪边"
     本来就是相机朝向，不是复制的身体朝向。细节见函数内注释。 */
  _fpvYawSource() {
    const k = this.followKey || "__self";
    if (k !== "__self") return this._followYaw();
    const s = this._snap;
    if (!s) return null;
    // 自机的水平朝向用**上行**的瞄准朝向（C2S ch3 自报，s.self_aim_yaw）：
    // 第一视角要的是"我在看哪边"，那是相机朝向，不是下行槽位复制的身体朝向。
    //
    // 与下行槽位 yaw 实测对比（一局 4215 对配对样本）：差中位 1.4°、p75 5.6°、
    // ≤10° 占 81% —— 大部分时候是同一个量；但转身/横移时会分叉（p90 27°、
    // p90 那档之外 p99 86°、最大 153°）。当初就是因为这个分叉判它"方向不对"
    // 退回下行的；现在上行位姿从 7.4Hz 提到 15.0Hz（见 c2s_self.parse_fgp 收下
    // lite 尾型），采样密一倍，重新启用。
    //
    // 拿不到就退回下行身体朝向（对局未开始 / 上行停了 / 位姿超过 10s 没更新，
    // 门槛见 er_radar._api_state_live）——宁可换一个口径，也不能让镜头卡在陈旧角度。
    // 回放路径不提供 self_aim_yaw，所以回放里自动走的就是下行那条。
    const aim = s.self_aim_yaw;
    if (typeof aim === "number" && isFinite(aim)) return aim;
    return s.self_yaw;
  }

  _followYaw() {
    const k = this.followKey || "__self";
    if (k === "__self") return (this._snap ? this._snap.self_yaw : null);
    const e = ((this._snap && this._snap.entities) || []).find(x => x.key === k);
    return e ? e.yaw : null;
  }

  /* 把一帧采样压进插值缓冲（FPV 分支每帧调用）。
     只收"值真的变了"的采样：前端是自适应轮询（数据没变时拿到同一份缓存 body），
     不筛的话缓冲会被同一个坐标灌满，插值就退化成停在原地。 */
  _fpvPush(p, yaw, pitch, srvTs) {
    const now = performance.now() / 1000;
    const b = this._fpvBuf;
    // 采样时刻：优先用**服务端给的 pose 时间**（精确间隔），否则退回"收到响应的时刻"
    // （被轮询网格量化，插值会有速度抖动 —— 见 _fpvDelay 注释里的实测）。
    // 两个时钟的差用"最小观测 delay"做锚（最小 = 延迟最小的那一次），再让它缓慢上漂
    // 跟住时钟漂移；服务端时间回退（换局/重连）则清缓冲重建。
    let t = now;
    if (typeof srvTs === "number" && isFinite(srvTs)) {
      const off = now - srvTs;
      if (this._fpvSrvOff == null || this._fpvLastSrv == null) this._fpvSrvOff = off;
      else if (srvTs < this._fpvLastSrv - 1.0) { b.length = 0; this._fpvSrvOff = off; }
      else if (off < this._fpvSrvOff) this._fpvSrvOff = off;
      // 上漂要慢：每样本的偏移修正等于把插值时间轴平移一下，修得太快就变成速度抖动
      // （实测 2%/样本 时残留 0.09m/s，0.5% 后降到 ~0.01）。20ppm 的时钟漂移远小于这个速率。
      else this._fpvSrvOff += (off - this._fpvSrvOff) * 0.005;
      this._fpvLastSrv = srvTs;
      t = srvTs + this._fpvSrvOff;
    } else {
      this._fpvSrvOff = null; this._fpvLastSrv = null;
    }
    const last = b.length ? b[b.length - 1] : null;
    const moved = !last || last.p.distanceTo(p) > 1e-4
      || (yaw != null && last.yaw != null && Math.abs(yaw - last.yaw) > 1e-3)
      || (pitch != null && last.pitch != null && Math.abs(pitch - last.pitch) > 1e-3);
    if (!moved) return;
    // 空档过大（换局/上行停发）→ 跨空档插值会横扫一整段，直接重建
    if (last && t - last.t > FPV_LERP_GAP_MAX) { b.length = 0; this._fpvCut = true; }
    // 采样级不连续（传送/换局/脏样本）→ 清缓冲，让相机直接切过去。阈值必须紧：
    // 真人两个采样之间不可能走出 5m。原来用 60m，于是几十米级的跳变被插值连起来
    // 横扫（实测 50m/s 的速度尖峰）。
    if (last && last.p.distanceTo(p) > FPV_SAMPLE_MAX_JUMP) { b.length = 0; this._fpvCut = true; }
    // 采样间隔只在这里（有真实采样时）更新并做 EMA：不能每帧从缓冲算中位数 ——
    // 间隔是 20/100ms 双峰，中位数会在两档之间翻，延迟跟着翻 = 时间轴抖 = 位置抖。
    if (last && t - last.t > 1e-4 && t - last.t < 1.0) {
      const g = t - last.t;
      this._fpvGap = this._fpvGap == null ? g : this._fpvGap + (g - this._fpvGap) * 0.15;
    }
    this._fpvLastPushT = now;
    b.push({ t, p: p.clone(), yaw, pitch });
    while (b.length > 64) b.shift();
    const cut = now - 1.5;
    while (b.length > 2 && b[0].t < cut) b.shift();
  }

  /* 插值延迟（秒）。**必须大于采样间隔**：延迟小于间隔时 (now-delay) 会落在最新
     采样之后，只能"保持最新值等下一个点"，插值就退化成"停一下、跳一下"—— 顿感照旧
     （这是实测出来的：delay=40ms 而间隔 50ms 时，每帧位移标准差只降到原来的 1/5，
     最大步长仍有 0.5m）。所以取 max(滑块给的值, 1.4×实测间隔中位)，上限 FPV_LERP_DELAY_MAX。
     滑块仍是"越大越顺滑、越小越跟手"：它给的是下限，自动那部分只保证不断档。 */
  _fpvDelay(tauMs) {
    // gap 用 _fpvPush 维护的 EMA（稳定），不再每帧从缓冲重算中位数 —— 后者会在双峰
    // 间隔之间翻档，而延迟每变一点就是把插值时间轴平移一点 = 看得见的位置抖。
    const gap = this._fpvGap || 0;
    return Math.min(FPV_LERP_DELAY_MAX,
                    Math.max(FPV_LERP_DELAY_MIN, tauMs / 1000, gap * FPV_LERP_DELAY_K));
  }

  /* 运动强度系数 0~1：乘在插值延迟上。0 = 直接跟最新采样（起步/停止立即响应），
     1 = 满延迟 + 插值（高速时抹平台阶）。带低通，避免阈值附近来回跳。 */
  _fpvMotionK(now) {
    const b = this._fpvBuf;
    let want = 0;
    if (b.length >= 2) {
      const last = b[b.length - 1], prev = b[b.length - 2];
      const dt = last.t - prev.t;
      const spd = dt > 1e-6 ? last.p.distanceTo(prev.p) / dt : 0;
      // 数据不再变化（服务器只在位置变化时才发 → 说明人停了）：直接把系数压到 0，
      // 否则"停不住"——插值会把最后一段速度放完才停。
      // 判据必须用「最后一次推入的客户端时刻」，不能用 last.t —— last.t 是服务端采样
      // 时刻映射过来的，now - last.t 约等于传输延迟，和 2×采样间隔同量级 → 会误翻转，
      // 而每次翻转都让延迟瞬间崩塌 = 位置跳（实测那几次 40m/s 尖峰就是这么来的）。
      const gap = this._fpvGap || 0;
      const since = now - (this._fpvLastPushT == null ? now : this._fpvLastPushT);
      const stale = gap > 1e-6 && since > FPV_LERP_STALE_K * gap;
      want = stale ? 0 : Math.max(0, Math.min(1, spd / FPV_LERP_SPEED_FULL));
    }
    const dtK = this._fpvDt > 1e-6 ? this._fpvDt : 1 / 60;
    const k = (this._fpvK == null) ? want
      : this._fpvK + (want - this._fpvK) * (1 - Math.exp(-dtK / FPV_LERP_K_TAU));
    this._fpvK = k;
    return k;
  }

  /* 取 (now - delay) 时刻的插值位姿；缓冲不足时退回最新（= 旧行为，只会更差不会更怪）。*/
  _fpvSample(delay) {
    const b = this._fpvBuf;
    if (!b.length) return null;
    const out = this._fpvSmp;
    const tt = performance.now() / 1000 - delay;
    const first = b[0], last = b[b.length - 1];
    if (b.length === 1 || tt <= first.t) {
      out.p.copy(first.p); out.yaw = first.yaw; out.pitch = first.pitch;
      return out;
    }
    if (tt >= last.t) {
      out.p.copy(last.p); out.yaw = last.yaw; out.pitch = last.pitch;
      return out;
    }
    let i = b.length - 1;
    while (i > 0 && b[i - 1].t > tt) i--;
    const a = b[i - 1], c = b[i];
    const span = c.t - a.t;
    const u = span > 1e-6 ? (tt - a.t) / span : 0;
    // Hermite（切线取中心差分），不是两点线性插值。
    // 原因：采样时间是**客户端收到响应的时刻**，被轮询节拍（50ms）量化，而数据本身
    // 是 42ms 均隔 —— 每个区间的"隐含速度"因此各自偏一点，两点线性插值会把这个
    // 偏差原样变成"这一段快、那一段慢"的微抖。用邻居算切线，速度在区间之间连续，
    // 抖动被邻居平均掉。p0/p1 是端点切线（末段退化成线性）。
    const prev = i >= 2 ? b[i - 2] : null;
    const next = i + 1 < b.length ? b[i + 1] : null;
    const m0 = this._vm0, m1 = this._vm1;
    const hs = span > 1e-6 ? span : 1;
    // 端点速度 = 中心差分；缺邻居就退化成该区间自身斜率（= 线性）
    if (prev && c.t - prev.t > 1e-6) m0.copy(c.p).sub(prev.p).divideScalar(c.t - prev.t);
    else m0.copy(c.p).sub(a.p).divideScalar(hs);
    if (next && next.t - a.t > 1e-6) m1.copy(next.p).sub(a.p).divideScalar(next.t - a.t);
    else m1.copy(c.p).sub(a.p).divideScalar(hs);
    hermite3(out.p, a.p, c.p, m0, m1, hs, u);
    // 角度走最短弧，否则 yaw 在 ±180 处会绕一整圈
    if (a.yaw != null && c.yaw != null) {
      let d = c.yaw - a.yaw;
      while (d > 180) d -= 360;
      while (d < -180) d += 360;
      out.yaw = a.yaw + d * u;
    } else out.yaw = (c.yaw != null ? c.yaw : a.yaw);
    out.pitch = (a.pitch != null && c.pitch != null)
      ? a.pitch + (c.pitch - a.pitch) * u
      : (c.pitch != null ? c.pitch : a.pitch);
    return out;
  }

  _loop() {
    this._raf = requestAnimationFrame(() => this._loop());
    const cam = this.camera;
    const now = performance.now();
    const dt = Math.min(0.05, Math.max(1e-3, (now - (this._fpvT || now)) / 1000));
    this._fpvT = now;
    const fpvTg = this.camMode === "fpv" ? this._fpvPosSource() : null;
    if (fpvTg) {
      const yaw = this._fpvYawSource();
      // 自机俯仰：只有上行（C2S ch3）能解出来，下行玩家状态复制里没有。
      // 拿不到就保持水平（0）——不是"卡在旧角度"，而是明确退回水平。
      const sp = this._snap && this._snap.self_pitch;
      const pitch = (typeof sp === "number" && isFinite(sp)) ? sp : null;
      // 平滑时间来自「第一视角平滑」：越大越顺滑、越小越跟手；下限见 FPV_TAU_MIN。
      let tauMs = Number(this.pref && this.pref.fpvtau);
      if (!Number.isFinite(tauMs)) tauMs = 80;
      tauMs = Math.max(FPV_TAU_MIN, tauMs);
      // 朝向平滑**不再**跟着位置滑块线性涨：转头是"瞄准"，60ms 以上必然被感知成延迟。
      // 上限 FPV_TAU_ROT_MAX，位置调到多大都不影响转头的跟手程度。
      const stRot = Math.min(FPV_TAU_ROT_MAX,
                             Math.max(FPV_TAU_YAW_MIN, tauMs * 0.75 / 1000));
      const nowS = now / 1000;
      // ── 插值：把相邻采样"连起来" ────────────────────────────────────────
      // update() 每次给的是一个**台阶**（15~20Hz）。原来直接用最新值当目标，靠临界
      // 阻尼去追：阻尼时间常数(80ms)比采样间隔(50ms)还长，相机永远追不上，每来一个
      // 新值就"起步—被打断—再起步"，那就是"一顿一顿"。
      // 现在改成标准实体插值：采样入缓冲，渲染时取 (now - delay) 时刻的插值位姿，
      // 相机在两次采样之间是匀速直线走过去的，运动连续。
      // delay 以「第一视角平滑」滑块为下限，并自动保证大于采样间隔（见 _fpvDelay）。
      // 延迟按运动强度缩放：静止/停住 → 0（起步与停止立即跟手），高速 → 满延迟（抹台阶）
      this._fpvDt = dt;
      const smp = this._fpvSample(this._fpvDelay(tauMs) * this._fpvMotionK(now / 1000));
      const tgt = smp ? smp.p : fpvTg;
      const tYaw = smp ? smp.yaw : yaw;
      const tPitch = smp ? smp.pitch : pitch;
      // 阻尼降级为"脏样本兜底"：时间轴精确后插值出来的运动本身就是均匀的（实测每帧
      // 速度标准差 0.0000），顺滑不再依赖阻尼 —— 所以这里给一个**很短**的固定值，
      // 只为挡住单个坏点（实测自机轨迹有 1~2m 级脏样本，全无阻尼会让镜头弹一下）。
      // 不再跟着滑块涨：滑块现在只决定插值延迟（= 顺滑 vs 跟手的取舍）。
      const stPos2 = FPV_DAMP_GUARD;
      // 数据断过（空档 / 采样级跳变）→ 直接切过去，不要用阻尼滑过去：实测 10m 的
      // 合法位移会让相机以 160m/s（FPV_MAX_SPEED 钳位）飞过去，那比硬切更难看。
      if (!this._fpvInited || this._fpvCut || this._fpvPos.distanceTo(tgt) > FPV_SNAP_DIST) {
        this._fpvCut = false;
        this._fpvPos.copy(tgt);
        this._fpvVel.set(0, 0, 0);
        if (tYaw != null) { this._fpvYaw = tYaw; this._fpvVelYaw = 0; }
        if (tPitch != null) { this._fpvPitch = tPitch; this._fpvVelPitch = 0; }
        this._fpvInited = true;
      } else {
        let r = smoothDamp(this._fpvPos.x, tgt.x, this._fpvVel.x, stPos2, dt, FPV_MAX_SPEED);
        this._fpvPos.x = r[0]; this._fpvVel.x = r[1];
        r = smoothDamp(this._fpvPos.y, tgt.y, this._fpvVel.y, stPos2, dt, FPV_MAX_SPEED);
        this._fpvPos.y = r[0]; this._fpvVel.y = r[1];
        r = smoothDamp(this._fpvPos.z, tgt.z, this._fpvVel.z, stPos2, dt, FPV_MAX_SPEED);
        this._fpvPos.z = r[0]; this._fpvVel.z = r[1];
        if (tYaw != null) {
          if (this._fpvYaw == null) { this._fpvYaw = tYaw; this._fpvVelYaw = 0; }
          else {
            const ry = smoothDampAngle(this._fpvYaw, tYaw, this._fpvVelYaw, stRot, dt, FPV_MAX_TURN);
            this._fpvYaw = ry[0]; this._fpvVelYaw = ry[1];
          }
        }
        if (tPitch != null) {
          if (this._fpvPitch == null) { this._fpvPitch = tPitch; this._fpvVelPitch = 0; }
          else {
            const rp = smoothDampAngle(this._fpvPitch, tPitch, this._fpvVelPitch, stRot, dt, FPV_MAX_TURN);
            this._fpvPitch = rp[0]; this._fpvVelPitch = rp[1];
          }
        } else if (this._fpvPitch) {
          const rh = smoothDampAngle(this._fpvPitch, 0, this._fpvVelPitch, stRot * 2, dt, FPV_MAX_TURN * 0.5);
          this._fpvPitch = rh[0]; this._fpvVelPitch = rh[1];
          if (Math.abs(this._fpvPitch) < 0.2) { this._fpvPitch = 0; this._fpvVelPitch = 0; }
        }
      }
      // 眼高可调（第一视角高度，米）：默认 1.6
      const eye = Number(this.pref && this.pref.fpvheight);
      const eyeY = (Number.isFinite(eye) && eye > 0.2) ? eye : 1.6;
      cam.position.set(this._fpvPos.x, this._fpvPos.y + eyeY, this._fpvPos.z);
      if (this._fpvYaw != null) {
        const r = this._fpvYaw * Math.PI / 180;
        const p = (this._fpvPitch || 0) * Math.PI / 180;
        const cp = Math.cos(p);
        cam.lookAt(this._fpvPos.x + Math.cos(r) * cp * 100,
                   this._fpvPos.y + eyeY + Math.sin(p) * 100,
                   this._fpvPos.z + Math.sin(r) * cp * 100);
      }
    } else {
      // 目标丢失（不在模型范围/换局）：清掉预测状态，别拿旧速度外推。
      // 不重置 _fpvInited：目标回来时距离近就滑过去、超过 FPV_SNAP_DIST 才直接对位。
      // 目标丢失：不做外推（已无预测状态），回到时按距离决定滑过去还是对位
      if (this.camMode === "chase" && this._followTarget()) {
        const yawDeg = this._followYaw();
        if (yawDeg != null) {
          const rr = yawDeg * Math.PI / 180;
          const want = Math.atan2(-Math.cos(rr), -Math.sin(rr));   // 相机吊在身后，看向自己面向
          let dy = want - this.orbit.yaw;
          while (dy > Math.PI) dy -= Math.PI * 2;
          while (dy < -Math.PI) dy += Math.PI * 2;
          if (Math.abs(dy) > 0.0015) {          // 收敛后不再动，避免画面抖
            this.orbit.yaw += dy * 0.15;
            this.orbit.dirty = true;
          }
        }
      }
      this.orbit.apply(cam);
    }
    const fpv = this.camMode === "fpv";
    // 视野可调（设置 → 视野 FOV）：同一个人物/距离，FOV 越大画面越广、目标越小。
    // 相机只有一个，所以三个 3D 视角共用（它在第一视角里最直观）。
    const wantFov = Number(this.pref && this.pref.fov);
    if (Number.isFinite(wantFov) && wantFov >= 40 && wantFov <= 140
        && Math.abs(cam.fov - wantFov) > 0.01) {
      cam.fov = wantFov;
      cam.updateProjectionMatrix();
    }
    // 第一视角把远平面收到雾外沿附近：贴地平视时远景全在雾里，远平面拉到几千
    // 只是白白多画一堆被雾吃掉的像素（这是"第一视角卡、其它视角不卡"的一大原因，
    // 因为轨道/俯视看全图需要远平面）。
    const wantFar = fpv ? 1100 : (this._farMax || 12000);
    if (Math.abs(cam.far - wantFar) > 1) {
      cam.far = wantFar;
      cam.updateProjectionMatrix();
    }
    // 帧率上限（设置 → 帧率上限，0 = 无上限）：按固定间隔出帧，避免 rAF 在
    // 144Hz 屏上跑出 90~144 的不均匀节拍（"卡卡的"有时只是节拍不齐，不是平均帧率低）。
    const cap = Number(this.pref && this.pref.fpscap) || 0;
    if (cap > 0 && now - (this._lastDraw || 0) < (1000 / cap) - 1) return;
    this._lastDraw = now;
    // 帧率（0.5s 滚动平均）：只统计真正出帧的次数（受上限/降档影响后的真实帧率）。
    this._fpsN = (this._fpsN || 0) + 1;
    if (now - (this._fpsT || now) >= 500) {
      this._stat.fps = Math.round(this._fpsN * 1000 / Math.max(1, now - this._fpsT));
      this._fpsT = now;
      this._fpsN = 0;
    }
    this._autoDowngrade();
    // 雾随视距自适应：俯视全图时不该被雾吃掉，贴地时又要有纵深
    const d = fpv ? 180 : this.orbit.dist;
    this.scene.fog.near = Math.max(40, d * 0.75);
    this.scene.fog.far = Math.max(400, d * 5.5);
    this.grid.visible = false;   // 不用地面网格线
    // 标记缩放：
    //  · 第一视角：保持真实透视（近大远小）—— 只在 300m 以外才稍微放大，免得太远
    //    缩成一个点。之前统一按 d/90 补偿，屏幕大小成了常数，第一视角里 50~540m 的
    //    人一样大，完全分不出远近。
    //  · 其它视角（自由/跟随/俯视）：维持"等屏幕大小"补偿，大图上远处标记也看得清。
    for (const mk of this._mk.values()) {
      const d = cam.position.distanceTo(mk.g.position);
      const ns = fpv ? Math.max(1, Math.min(3, d / 300))
                     : Math.max(0.55, Math.min(6, d / 90));
      // 变化不到 1% 就不写：setScalar 会让 three 重算矩阵，几百个标记每帧全写纯浪费
      if (!mk._gs || Math.abs(ns - mk._gs) > mk._gs * 0.01) {
        mk.g.scale.setScalar(ns);
        mk._gs = ns;
      }
    }
    this.renderer.render(this.scene, cam);
    this._drawLabels();
    this._drawWarn();
  }

  // 同一个投影/摆放逻辑伺候两种标签：头顶信息行（底边贴锚点）与脚下距离
  // （顶边贴锚点）。_top=1 的是脚下距离。
  _placeLabel(el, v, w, h) {
    v.copy(el._p).project(this.camera);
    // 视口外直接不写 DOM。原来只判了深度，屏幕外的标签每帧照样写 transform ——
    // 第一视角是最窄的视锥（FOV 55~120°），绝大多数实体都在屏幕外，这正是
    // "只有第一视角卡"的主要每帧开销。
    if (v.z > 1 || v.z < -1 || v.x < -1.15 || v.x > 1.15 || v.y < -1.15 || v.y > 1.15) {
      if (!el._hid) { el.style.display = "none"; el._hid = true; el._px = null; }
      return;
    }
    if (el._hid) { el.style.display = ""; el._hid = false; }
    const px = (v.x * 0.5 + 0.5) * w, py = (-v.y * 0.5 + 0.5) * h;
    // 位移不到半像素就不写：transform 会触发合成层更新，静止/慢走时白写
    if (el._px == null || Math.abs(px - el._px) > 0.5 || Math.abs(py - el._py) > 0.5) {
      el._px = px; el._py = py;
      el.style.transform = (el._top ? "translate(-50%,0)" : "translate(-50%,-100%)")
        + " translate3d(" + px.toFixed(1) + "px," + py.toFixed(1) + "px,0)";
    }
    const c = "#" + (el._c || 0xffffff).toString(16).padStart(6, "0");
    if (el._cc !== c) { el.style.color = c; el._cc = c; }
  }

  /* ------------------------------------------------------- 屏幕外预警 */
  /* 贴近我、又不在画面里的敌人 → 在屏幕中心的基准圆上画一个指向它的箭头，
     箭头颜色跟着该敌人的队伍色，旁边标剩余距离。距离阈值**复用 2D 的
     「贴脸高亮」**（pref.alert，米），圆半径由「预警圆半径」给（pref.warnr，px）。
     必须放在 renderer.render() 之后调用：那时相机的 matrixWorldInverse 才是
     本帧的（apply/lookAt 只是写矩阵，要 updateMatrixWorld 之后才可用于投影）。 */
  _drawWarn() {
    const svg = this.warnSvg;
    if (!svg) return;
    const P = this.pref || {};
    const warnD = Number(P.warnd) || 0;      // 画箭头的距离（自己的设置，与贴脸解耦）
    const alertR = Number(P.alert) || 0;     // 贴脸距离：只决定箭头前那个小红点
    const cam = this.camera;
    // 箭头大小（%）：三角、红点、数字一起缩放；圆半径独立，两者分开调
    const SC = Math.max(0.3, Math.min(3, (Number(P.warnsz) || 100) / 100));
    const items = [];
    let R = 200, cxp = 0, cyp = 0;
    if (P.warn3d && warnD > 0 && this._snap && cam) {
      const w = this.canvas.clientWidth || 1, h = this.canvas.clientHeight || 1;
      cxp = w / 2; cyp = h / 2;
      // 半径上限要**连箭头和红点一起**留出空间：三角外伸 13·SC、再留 6·SC 间隙、
      // 红点半径 3.4·SC。只按圆的尺寸收，箭头+红点会被推出画面。
      const tipOut = 13 * SC + 6 * SC + 3.4 * SC;
      R = Math.max(40, Math.min(Number(P.warnr) || 200, Math.min(w, h) * 0.5 - 14 - tipOut));
      const cs = this._tmpW || (this._tmpW = new THREE.Vector3());
      const pr = this._tmpW2 || (this._tmpW2 = new THREE.Vector3());
      for (const e of this._snap.entities || []) {
        if (!e.world) continue;
        if (!((e.kind === "player" && e.team > 0) || e.kind === "ai")) continue;
        if (e.dead || e.spawn_mark) continue;      // 同 2D 贴脸：只认实时敌人
        // 距离口径与 2D 的 dist2 一致：相对自机的水平距离（米）
        if (!e.rel) continue;
        const dx = e.rel[0], dy = e.rel[1];
        if (!Number.isFinite(dx) || !Number.isFinite(dy)) continue;
        const d = Math.hypot(dx, dy) / 100;
        if (d > warnD) continue;
        // 不调 wpos()：它每次返回新 Vector3，这个循环每帧跑，临时向量复用
        const wx = e.world[0] * W2M, wy = e.world[2] * W2M, wz = e.world[1] * W2M;
        pr.set(wx, wy, wz).applyMatrix4(cam.matrixWorldInverse);
        const behind = pr.z >= 0;                  // three 的相机看向 -Z
        if (!behind) {
          // 在画面里就交给人物标记本身，不再画箭头
          cs.set(wx, wy, wz).project(cam);
          if (cs.x >= -1.02 && cs.x <= 1.02 && cs.y >= -1.02 && cs.y <= 1.02) continue;
        }
        // 屏幕方向 = (相机空间 x, -相机空间 y)：对"身前但出画"和"在身后"都成立。
        // NDC 在目标位于相机背后会翻面，拿它算方向会把身后的敌人指反。
        let ax = pr.x, ay = -pr.y;
        if (Math.hypot(ax, ay) < 1e-4) { ax = 0; ay = 1; }   // 正后方：固定朝下，别乱转
        items.push({ d, a: Math.atan2(ay, ax), c: colorOf(e),
                     near: alertR > 0 && d <= alertR });      // near = 已贴脸 → 加红点
      }
      items.sort((p, q) => p.a - q.a);             // 定序，避免相邻箭头每帧交换位置
    }
    const NS = "http://www.w3.org/2000/svg";
    // 基准圆：常驻一个元素，只切显隐（开了关、关了开不必重建）
    let ring = this._warnRing;
    if (!ring) {
      ring = this._warnRing = document.createElementNS(NS, "circle");
      ring.setAttribute("class", "ring");
    }
    if (ring.parentNode !== svg) svg.insertBefore(ring, svg.firstChild);
    const show = items.length > 0;
    const wantDisp = show ? "" : "none";
    if (ring.style.display !== wantDisp) ring.style.display = wantDisp;
    if (show) {
      ring.setAttribute("cx", cxp); ring.setAttribute("cy", cyp); ring.setAttribute("r", R);
    }
    // 箭头池：每条一个三角 + 一个距离数字 + 一个贴脸红点，数量变了才增删
    const pool = this._warnPool || (this._warnPool = []);
    while (pool.length < items.length) {
      const p = document.createElementNS(NS, "path"); p.setAttribute("class", "arw");
      const dot = document.createElementNS(NS, "circle"); dot.setAttribute("class", "dotw");
      pool.push({ p, t: document.createElementNS(NS, "text"), dot });
    }
    while (pool.length > items.length) {
      const o = pool.pop(); o.p.remove(); o.t.remove(); o.dot.remove();
    }
    for (let i = 0; i < items.length; i++) {
      const o = pool[i], it = items[i];
      if (o.p.parentNode !== svg) { svg.appendChild(o.p); svg.appendChild(o.t); svg.appendChild(o.dot); }
      const deg = it.a * 180 / Math.PI;
      const hex = "#" + it.c.toString(16).padStart(6, "0");
      // 三角在 rotate 后的局部坐标里朝 +X 指出去，贴在圆周外侧
      o.p.setAttribute("d", "M" + R + "," + (-7 * SC) + " L" + (R + 13 * SC) + ",0 L" + R + "," + (7 * SC) + " Z");
      o.p.setAttribute("transform", "translate(" + cxp + "," + cyp + ") rotate(" + deg.toFixed(1) + ")");
      o.p.setAttribute("fill", hex);
      // 贴脸红点：画在箭头**前方**（比三角顶点更外侧），表示"这个方向的人已经很近"。
      // 只在贴脸距离内出现；贴脸关了（alert=0）就不画。
      if (it.near) {
        const rr = R + 13 * SC + 6 * SC;
        o.dot.setAttribute("cx", (cxp + rr * Math.cos(it.a)).toFixed(1));
        o.dot.setAttribute("cy", (cyp + rr * Math.sin(it.a)).toFixed(1));
        o.dot.setAttribute("r", (3.4 * SC).toFixed(2));
        if (o.dot.style.display === "none") o.dot.style.display = "";
      } else if (o.dot.style.display !== "none") {
        o.dot.style.display = "none";
      }
      o.t.setAttribute("x", (cxp + (R - 17 * SC) * Math.cos(it.a)).toFixed(1));
      o.t.setAttribute("y", (cyp + (R - 17 * SC) * Math.sin(it.a)).toFixed(1));
      o.t.setAttribute("fill", hex);
      // 字号走内联（样式表里是 font 简写，内联 font-size 能盖住）
      o.t.style.fontSize = (11 * SC).toFixed(1) + "px";
      const txt = it.d.toFixed(0) + "m";
      if (o.t.textContent !== txt) o.t.textContent = txt;   // textContent 赋值也会触发重排
    }
  }

  _drawLabels() {
    if (!this.labels) return;
    // 标签最多 ~66Hz 更新一次：高刷屏（120/144）下 3D 场景照跑，但 DOM 写入减半 ——
    // 标签是文字，60Hz 的跟随肉眼看不出与 120Hz 的差别。
    const tn = performance.now();
    if (tn - (this._lblT || 0) < 15) return;
    this._lblT = tn;
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    const v = new THREE.Vector3();
    const hideKey = this.camMode === "fpv" ? (this.followKey || "__self") : null;
    for (const el of this._lbl.values()) {
      if (el._k === hideKey || !el._p) {
        if (!el._hid) { el.style.display = "none"; el._hid = true; el._px = null; }
        continue;
      }
      this._placeLabel(el, v, w, h);
    }
    for (const el of this._lblD.values()) {
      if (el._k === hideKey || !el._p) {
        if (!el._hid) { el.style.display = "none"; el._hid = true; el._px = null; }
        continue;
      }
      this._placeLabel(el, v, w, h);
    }
  }

  stat() { return this._stat; }
  dispose() {
    cancelAnimationFrame(this._raf);
    for (const el of this._lbl.values()) el.remove();
    this._lbl.clear();
    for (const el of this._lblD.values()) el.remove();
    this._lblD.clear();
    this.renderer.dispose();
  }
}

export function create(opts) { return new R3D(opts); }
export { wpos, colorOf, parseGLB, THREE };
