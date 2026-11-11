import * as THREE from 'three';
import {characterKind,aimOf,PosePresenter} from './gateway-pose.js?v=1.1.0';
import { parseGLB } from './r3d.js?v=1.1.0';
import { gateway } from './korr-renderer.js?v=1.1.0';

// ER snapshots and local GLBs -> Korr renderer, retaining the ER page and controls.
// Coordinates enter in UE centimetres. GLB uses metres [UE.x, UE.z, UE.y].
export function interpolateHumans(previous, current, fraction) {
  if (!previous || !current || current.replay?.seeking || current.replay?.paused) return current;
  const t = Math.max(0, Math.min(1, fraction));
  const old = new Map((previous.entities || []).map(e => [e.key, e]));
  const blend = (a, b) => {
    if (!a?.world || !b?.world || Math.hypot(...a.world.map((x,i) => x-b.world[i])) > 3000) return b;
    const world = b.world.map((v,i) => a.world[i] + (v-a.world[i])*t);
    let yaw = b.yaw;
    if (Number.isFinite(a.yaw) && Number.isFinite(b.yaw)) {
      const delta = ((b.yaw-a.yaw+540)%360)-180;
      yaw = a.yaw + delta*t;
    }
    return {...b, world, xyz:world, yaw};
  };
  const entities = (current.entities || []).map(e => ['player','mate'].includes(e.kind) && !e.is_bot ? blend(old.get(e.key),e) : e);
  const own = blend({world:previous.self,yaw:previous.self_aim_yaw ?? previous.self_yaw},
                    {world:current.self,yaw:current.self_aim_yaw ?? current.self_yaw});
  return {...current,entities,self:own.world,self_yaw:own.yaw,self_aim_yaw:own.yaw};
}

export function toKorr(snapshot, pref={}) {
  const hp = h => {
    if (h && Array.isArray(h.total)) return {hp:h.total[0],maxHp:h.total[1]};
    return Number.isFinite(h) ? {hp:h,maxHp:100} : {hp:null,maxHp:null};
  };
  const entity = e => ({...e, x:e.world[0], y:e.world[1], z:e.world[2],
    // Follow targets use stable actor keys, labels retain the real name.
    name:e.key, displayName:e.name === '物资' ? '未知物资 · ID待识别' : e.name || '未命名', ...hp(e.hp),
    alive:!e.dead, quality:e.grade ?? 0, weapon:e.curr_weapon || e.weapon,
    helmetLv:e.helmet, armorLv:e.vest,helmetDur:e.helmet_dur,armorDur:e.vest_dur,hero:e.hero});
  const valid = (snapshot.entities || []).filter(e => Array.isArray(e.world) && e.world.length>=3 && e.world.every(Number.isFinite));
  return {players:valid.filter(e=>characterKind(e) && (pref.mate!==0 || e.kind!=='mate')).map(entity),
    ais:valid.filter(e=>e.kind==='ai').map(entity),bosses:[],
    items:valid.filter(e=>(pref.loot!==0 && e.kind==='loot') || (pref.box!==0 && e.kind==='box' && (pref.aibox!==0 || (!e.is_ai && !e.is_bot))) || (pref.container!==0 && e.kind==='container')).map(entity),
    local:snapshot.self ? {key:'__self',name:'__self__',displayName:snapshot.self_name || '自己',
      x:snapshot.self[0],y:snapshot.self[1],z:snapshot.self[2],...aimOf(snapshot),...hp(snapshot.self_hp)} : null,
    replay:!!snapshot.replay,slowRevision:Math.floor((snapshot.replay?.position || 0)*5)};
}

function geometryFromParts(parts) {
  let vertices=0, indices=0;
  for(const p of parts){vertices+=p.geometry.attributes.position.count;indices+=p.geometry.index.count;}
  const xyz=new Float32Array(vertices*3), ix=new Uint32Array(indices);
  const v=new THREE.Vector3();let base=0, at=0;
  for(const p of parts){
    const pos=p.geometry.attributes.position;
    for(let n=0;n<pos.count;n++){
      v.set(pos.getX(n),pos.getY(n),pos.getZ(n)).applyMatrix4(p.matrix);
      xyz[(base+n)*3]=v.x;xyz[(base+n)*3+1]=-v.z;xyz[(base+n)*3+2]=v.y;
    }
    for(let n=0;n<p.geometry.index.count;n++)ix[at++]=base+p.geometry.index.getX(n);
    base+=pos.count;p.geometry.dispose();
  }
  const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(xyz,3));g.setIndex(new THREE.BufferAttribute(ix,1));return g;
}

let initialized=false;
export function create(options) {
  window.AppState={gameData:null,frameCount:0,display:{}};
  window.viewMode='3d';
  window.isRadarTeammate = p => p.kind==='mate';
  window.showRadarToast=options.onToast;
  if(!initialized){gateway.init();initialized=true;}
  let latest=null,pref=options.pref || {},active=true;
  const presenter=new PosePresenter();
  let statError='',mapUrl='',pendingMap=null,loading=null,followKey='__self';
  const adapter={camMode:'chase',onCam:null,
    update(s){
      latest=s;presenter.push(s,performance.now());
      window.AppState.gameData=toKorr(presenter.sample(performance.now()),pref);window.AppState.frameCount++;
    },
    setPrefs(p){pref=p || {};window.AppState.display={showPlayers:true,showAIs:pref.ai!==0,
      showItems:pref.loot!==0 || pref.box!==0 || pref.container!==0,showHealth:pref.hp!==0,showName:pref.name!==0,
      showHero:pref.opname!==0,showWeapon:pref.wpn!==0,showArmor:pref.gear!==0,showDistance:pref.dist!==0,showCone:pref.cone!==0,
      eyeHeight:pref.fpvheight,fpvtau:pref.fpvtau,followYaw:['fpv','chase'].includes(pref.cam3d),fpscap:pref.fpscap,fontScale:(pref.fontsize||100)/100,charScale:(pref.charsize||100)/100,model3d:pref.model3d,visibleColor3d:pref.visiblecolor3d,occludedColor3d:pref.occludedcolor3d,directionStyle3d:pref.directionstyle3d,directionAnchor3d:pref.directionanchor3d,minQuality:0,minPrice:0};
      gateway.preferences(pref);
      if(latest){window.AppState.gameData=toKorr(presenter.sample(performance.now()),pref);window.AppState.frameCount++;}},
    setActive(on){active=on;window.viewMode=on?'3d':'2d';window.dispatchEvent(new CustomEvent('viewModeChanged',{detail:window.viewMode}));},
    resize(){gateway.resize();},
    stat(){return {...gateway.stat(),err:statError,engine:'Korr/Three.js',interpolation:'human-only',source:latest?.cursor,expectedCharacters:window.AppState.gameData?.players.length||0};},
    setQuality(q){window.radar3dSetQuality(({perf:'performance',low:'performance',mid:'balanced',high:'high'})[q] || q || 'auto');},
    setCam(mode){this.camMode=mode;window.AppState.display.followYaw=['fpv','chase'].includes(mode);
      if(mode==='top'){gateway.top();return;}
      window.radar3dSetCameraMode(mode==='orbit'?'free':mode==='fpv'?'firstPerson':'thirdPerson');
      if(mode!=='orbit')this.setFollowKey(followKey);
    },
    setFollowKey(key){followKey=key;const e=latest?.entities?.find(e=>e.key===key);gateway.follow(key==='__self'?'self':e?.kind==='ai'?'ai':'player',key==='__self'?'__self__':key);},
    focus(key,s){const e=key==='__self'?{world:s.self}:s.entities.find(x=>x.key===key);
      if(e?.world){window.radar3dSetCameraMode('free');window.radar3dFocus(key==='__self'?'self':'player',...e.world,key);this.camMode='orbit';}},
    async setMap(info,url,progress,model){
      pendingMap={info,url,progress,model};if(loading)return loading;
      loading=(async()=>{while(pendingMap){const job=pendingMap;pendingMap=null;if(job.url===mapUrl)continue;
        statError='';try{
          const response=await fetch(job.url,{cache:'force-cache'});if(!response.ok)throw Error('地图 HTTP '+response.status);
          const data=await response.arrayBuffer();job.progress?.(data.byteLength,data.byteLength);
          const geometry=geometryFromParts(parseGLB(data));gateway.installGeometry(job.info?.key || 'local',geometry);
          mapUrl=job.url;
          if(['orbit','top'].includes(adapter.camMode) || !latest?.self)gateway.fit();
        }catch(e){statError=String(e.message || e);options.onToast?.('3D 地图加载失败：'+statError);}
      }})();try{await loading;}finally{loading=null;}
    }
  };
  adapter.setPrefs(pref);
  function animate(){
    if(active && latest?.replay && !latest.replay.seeking){
      const sample=presenter.sample(performance.now());
      window.AppState.gameData=toKorr(sample,pref);
    }
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);
  window.gateway3d=adapter;
  return adapter;
}

export {geometryFromParts};
