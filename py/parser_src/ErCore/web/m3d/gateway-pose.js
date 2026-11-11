// Pure snapshot/pose contract shared by the adapter, renderer and regression tests.
export const characterKind = e => ['player','mate','unknown'].includes(e.kind);
export const validWorld = w => Array.isArray(w) && w.length >= 3 && w.slice(0,3).every(Number.isFinite);
export const scenePosition = w => [w[0]/100,-w[1]/100,w[2]/100];
export function aimOf(s) {
  const fresh = Number.isFinite(s.self_aim_yaw) && (!Number.isFinite(s.self_aim_age_ms) || s.self_aim_age_ms <= 250);
  return {yaw:fresh?s.self_aim_yaw:s.self_yaw, pitch:Number.isFinite(s.self_pitch)?s.self_pitch:0};
}
export function aimDirection(yaw, pitch=0) {
  const y=-yaw*Math.PI/180, p=Math.max(-89.9,Math.min(89.9,pitch))*Math.PI/180;
  return [Math.cos(y)*Math.cos(p),Math.sin(y)*Math.cos(p),Math.sin(p)];
}
export function blendPose(a,b,t) {
  if (a && b && ['eid','actor_guid','spawn_seq','kind','name'].some(k=>a[k]!==b[k])) return b;
  if (!validWorld(a?.world) || !validWorld(b?.world) || Math.hypot(...a.world.map((v,i)=>v-b.world[i]))>500) return b;
  const world=b.world.map((v,i)=>a.world[i]+(v-a.world[i])*t);
  const yaw=Number.isFinite(a.yaw)&&Number.isFinite(b.yaw)?a.yaw+(((b.yaw-a.yaw+540)%360)-180)*t:b.yaw;
  const pitch=Number.isFinite(a.pitch)&&Number.isFinite(b.pitch)?a.pitch+(b.pitch-a.pitch)*t:b.pitch;
  return {...b,world,xyz:world,yaw,pitch};
}

// Keep one continuous presentation timeline. Repeated snapshots cannot restart it,
// and new targets start at the currently displayed pose, never the previous target.
export class PosePresenter {
  constructor(){this.current=null;this.previous=null;this.at=0;this.duration=0;this.signature='';}
  push(s,now){
    const identity=[s.session,s.epoch,s.flow,s.local,s.remote].join('|');
    const reset=identity!==this.identity || s.replay?.seeking || s.replay?.paused ||
      s.replay?.position < (this.current?.replay?.position ?? 0);
    const sig=JSON.stringify([s.self,aimOf(s),s.entities?.map(e=>[e.key,e.kind,e.is_bot,e.world,e.yaw,e.pose_seq])]);
    if(!this.current || reset){this.previous=null;this.duration=0;this.at=now;}
    else if(sig!==this.signature){this.previous=this.sample(now);this.duration=Math.min(80,Math.max(16,now-this.at));this.at=now;}
    this.current=s;this.identity=identity;this.signature=sig;
  }
  sample(now){
    const s=this.current;
    if(!s || !this.previous || !s.replay || s.replay.paused || s.replay.seeking || !this.duration)return s;
    const t=Math.max(0,Math.min(1,(now-this.at)/this.duration));
    const old=new Map((this.previous.entities||[]).map(e=>[e.key,e]));
    const entities=(s.entities||[]).map(e=>characterKind(e)&&!e.is_bot?blendPose(old.get(e.key),e,t):e);
    const own=blendPose({world:this.previous.self,...aimOf(this.previous)},{world:s.self,...aimOf(s)},t);
    return {...s,entities,self:own.world,self_yaw:own.yaw,self_aim_yaw:own.yaw,self_pitch:own.pitch};
  }
}

// Partition index buffers only: retain every triangle and the shared world vertices.
export function splitSurfaceIndices(xyz, indices) {
  indices ??= Uint32Array.from({length:xyz.length/3},(_,i)=>i);
  const groups=new Uint8Array(indices.length/3);let horizontal=0;
  for(let i=0;i<indices.length;i+=3){
    const a=indices[i]*3,b=indices[i+1]*3,c=indices[i+2]*3;
    const ux=xyz[b]-xyz[a],uy=xyz[b+1]-xyz[a+1],uz=xyz[b+2]-xyz[a+2];
    const vx=xyz[c]-xyz[a],vy=xyz[c+1]-xyz[a+1],vz=xyz[c+2]-xyz[a+2];
    const nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
    const isFloor=Math.abs(nz)>=.70*Math.hypot(nx,ny,nz);
    groups[i/3]=isFloor?0:1;if(isFloor)horizontal+=3;
  }
  const result=new Uint32Array(indices.length);let floor=0,wall=horizontal;
  for(let i=0;i<indices.length;i+=3){const at=groups[i/3]?wall:floor;result.set(indices.subarray(i,i+3),at);if(groups[i/3])wall+=3;else floor+=3;}
  return {indices:result,horizontal,vertical:indices.length-horizontal};
}
