import * as THREE from 'three';
export const MODEL_IDS=['tactical','mannequin','beacon','capsule'];
const CAPSULE_FOOT_Z=-.9;
export function buildCharacterModel(style,options={}){
 const occluded=!!options.occluded;
 const root=new THREE.Group(), colored=[],geometries=[],materials=[];
 let body,dark,visor;
 if(occluded){
   body=new THREE.MeshBasicMaterial({color:0xff4058,depthTest:true,depthFunc:THREE.GreaterDepth,depthWrite:false,transparent:true,opacity:.72});
   dark=body;visor=body;materials.push(body);
 }else{
   body=new THREE.MeshStandardMaterial({color:0x49d5ff,roughness:.68,metalness:.15});
   dark=new THREE.MeshStandardMaterial({color:0x182530,roughness:.8});
   visor=new THREE.MeshStandardMaterial({color:0x9defff,emissive:0x184358,roughness:.2,metalness:.45});
   materials.push(body,dark,visor);
 }
 const part=(g,m,pos,rotation=null)=>{geometries.push(g);const mesh=new THREE.Mesh(g,m);mesh.position.set(...pos);if(rotation)mesh.rotation.set(...rotation);mesh.userData.radarExcludeFromSsao=occluded;mesh.userData.radarLitSurface=!occluded;mesh.userData.radarEntitySurface=!occluded;if(occluded)mesh.renderOrder=1001;root.add(mesh);if(m===body)colored.push(mesh);return mesh;};
 const box=(size,pos,m=body)=>part(new THREE.BoxGeometry(...size),m,pos);
 const sphere=(r,pos,m=body)=>part(new THREE.SphereGeometry(r,12,8),m,pos);
 const limb=(r,length,pos,m=body)=>part(new THREE.CapsuleGeometry(r,length,3,8),m,pos,[Math.PI/2,0,0]);
 if(style==='beacon'){
   part(new THREE.CylinderGeometry(.30,.38,.12,24),dark,[0,0,-.83],[Math.PI/2,0,0]);
   part(new THREE.OctahedronGeometry(.42,0),body,[0,0,.06]);
   part(new THREE.ConeGeometry(.20,.40,3),body,[.32,0,.67],[0,0,-Math.PI/2]);
 }else{
   const tactical=style==='tactical';
   if(tactical){box([.32,.52,.55],[0,0,.13]);box([.12,.39,.30],[.20,0,.12],dark);box([.20,.37,.40],[-.23,0,.13],dark);}
   else limb(.22,.20,[0,0,.13]);
   sphere(.19,[0,0,.68]);
   if(tactical){box([.045,.29,.095],[.18,0,.70],visor);box([.08,.40,.12],[.22,0,-.02],dark);}
   for(const sign of [-1,1]){
     limb(.075,.37,[0,sign*.34,.08],tactical?dark:body);
     sphere(.083,[.03,sign*.34,-.18]);
     limb(.095,.39,[0,sign*.14,-.44],tactical?dark:body);
     box([.32,.18,.14],[.06,sign*.14,-.81],dark);
   }
 }
 root.updateMatrixWorld(true);
 const footZ=new THREE.Box3().setFromObject(root).min.z;
 root.userData.style=style;
 root.userData.occluded=occluded;
 root.userData.footZ=footZ;
 return {root,footZ,setColor(c){body.color.setHex(c);},dispose(){for(const g of geometries)g.dispose();for(const m of new Set(materials))m.dispose();}};
}
export function syncCharacterModel(entity,style,options={}){
 const id=MODEL_IDS.includes(style)?style:'tactical';
 if(entity.modelStyle!==id){
   if(entity.customModel){entity.root.remove(entity.customModel.root);entity.customModel.dispose();entity.customModel=null;}
   if(entity.customOccludedModel){entity.root.remove(entity.customOccludedModel.root);entity.customOccludedModel.dispose();entity.customOccludedModel=null;}
   if(id!=='capsule'){
     entity.customModel=buildCharacterModel(id);entity.root.add(entity.customModel.root);
     entity.customOccludedModel=buildCharacterModel(id,{occluded:true});entity.root.add(entity.customOccludedModel.root);
   }
   entity.modelStyle=id;
 }
 const scale=THREE.MathUtils.clamp(Number(options.scale)||1,.5,2);
 const visibleColor=Number.isFinite(options.visibleColor)?options.visibleColor:entity.capsule.material.color.getHex();
 const occludedColor=Number.isFinite(options.occludedColor)?options.occludedColor:(entity.occCapsule?.material.color.getHex()??0xff4058);
 entity.root.scale.setScalar(1);
 entity.capsule.visible=id==='capsule';
 entity.capsule.scale.setScalar(scale);entity.capsule.position.z=CAPSULE_FOOT_Z-CAPSULE_FOOT_Z*scale;
 if(entity.occCapsule){entity.occCapsule.visible=id==='capsule';entity.occCapsule.scale.setScalar(scale);entity.occCapsule.position.z=CAPSULE_FOOT_Z-CAPSULE_FOOT_Z*scale;entity.occCapsule.material.color.setHex(occludedColor);}
 const directionStyle=options.directionStyle==='line'?'line':'arrow';
 const directionAnchor=options.directionAnchor==='foot'?'foot':'head';
 const headingZ=directionAnchor==='foot'?CAPSULE_FOOT_Z+.08:CAPSULE_FOOT_Z+1.8*scale;
 for(const [headingId,pair] of Object.entries(entity.headings||{}))for(const [pass,mesh] of Object.entries(pair)){
  mesh.visible=options.showHeading!==false&&headingId===directionStyle;
  mesh.scale.setScalar(scale);
  mesh.position.set((headingId==='line'?.9:1.1)*scale,0,headingZ);
  mesh.material.color.setHex(pass==='occluded'?occludedColor:visibleColor);
 }
 if(entity.hpBar?.group){entity.hpBar.group.scale.setScalar(scale);entity.hpBar.group.position.x=-.55*scale;}
 for(const model of [entity.customModel,entity.customOccludedModel])if(model){model.root.scale.setScalar(scale);model.root.position.z=CAPSULE_FOOT_Z-model.footZ*scale;}
 entity.capsule.material.color.setHex(visibleColor);
 entity.customModel?.setColor(visibleColor);
 entity.customOccludedModel?.setColor(occludedColor);
 entity.modelVisibleColor=visibleColor;entity.modelOccludedColor=occludedColor;
 entity.modelFootZ=CAPSULE_FOOT_Z;
 entity.headingStyle=directionStyle;entity.headingAnchor=directionAnchor;
 entity.modelRenderedFootZ=id==='capsule'
  ? entity.capsule.position.z+CAPSULE_FOOT_Z*scale
  : entity.customModel.root.position.z+entity.customModel.footZ*scale;
}
