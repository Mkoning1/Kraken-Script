import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

// Game visuals only. The 3D town never places orders or mutates trading data.
const colors={
 grass:0x91ba76,grass2:0x76a669,grass3:0x649057,water:0x478ea5,waterLight:0x73bcc3,
 road:0xe1c99d,roadBorder:0xb49d76,stone:0xc2b9a6,stoneDark:0x8f968d,
 wall:0xc3c2b3,timber:0x855b3e,roof:0xb95037,roofDark:0x61423a,
 blue:0x2a5b87,blueLight:0x416f9a,gold:0xf5c86b,leaf:0x3f8150,leaf2:0x679550,
 plaster:0xf7e4bb,sky:0xb0d7da,red:0xd75b51,white:0xf6e6b9
};
const mat=(hex,roughness=1)=>new THREE.MeshStandardMaterial({color:hex,roughness,flatShading:true});
const M=Object.fromEntries(Object.entries(colors).map(([name,hex])=>[name,mat(hex)]));
const groundMat=mat(0x668c5d),woodMat=mat(0x9b7144),sandMat=mat(0xd7c29d);
const flagGold=mat(0xf6d56f),windowMat=new THREE.MeshStandardMaterial({color:0x436f87,roughness:.35,emissive:0x1b425a,emissiveIntensity:.12});
const selectable=new Map(), anchors=new Map(), citizens=[],boats=[],windmills=[],flags=[],waves=[],particles=[];
let renderer,camera,controls,scene,container,raycaster,mouse,worldRoot,selected='townhall',run=true,last=0,tick=0;
let tracking={active:false,focus:null};
const clip=(v,min,max)=>Math.min(max,Math.max(min,v));
const rng=(()=>{let state=789321;return()=>((state=(state*1664525+1013904223)>>>0)/4294967296)})();
function mesh(geo,m,x=0,y=0,z=0,parent=worldRoot){const o=new THREE.Mesh(geo,m);o.position.set(x,y,z);parent.add(o);o.castShadow=true;o.receiveShadow=true;return o}
function box(w,h,d,m,x,y,z,parent){return mesh(new THREE.BoxGeometry(w,h,d),m,x,y,z,parent)}
function cyl(top,bottom,h,segments,m,x,y,z,parent){return mesh(new THREE.CylinderGeometry(top,bottom,h,segments),m,x,y,z,parent)}
function cone(r,h,segments,m,x,y,z,parent){return mesh(new THREE.ConeGeometry(r,h,segments),m,x,y,z,parent)}
function path(x1,z1,x2,z2,width=1.5){let dx=x2-x1,dz=z2-z1,len=Math.hypot(dx,dz);const road=box(width,.07,len,M.road,(x1+x2)/2,.15,(z1+z2)/2);road.rotation.y=Math.atan2(dx,dz);let edge=box(width+.23,.04,len+.08,M.roadBorder,(x1+x2)/2,.105,(z1+z2)/2);edge.rotation.y=road.rotation.y;}
function plaza(x,z,w,d){box(w,.09,d,M.road,x,.16,z);for(let i=0;i<17;i++){let xx=x+(rng()-.5)*w,zz=z+(rng()-.5)*d;box(.16,.015,.17,M.roadBorder,xx,.216,zz)}}
function shrub(x,z,r=.6){const trunk=cyl(.095,.11,r*1.5,6,woodMat,x,.55+r*.43,z);cone(r*1.05,r*2.1,6,rng()>.5?M.leaf:M.leaf2,x,1.2+r*.85,z);}
function tree(x,z,scale=1){cyl(.19*scale,.25*scale,1.8*scale,6,woodMat,x,.96*scale,z);cone(1.05*scale,2.25*scale,7,M.leaf,x,2.3*scale,z);cone(.87*scale,1.7*scale,7,M.leaf2,x,3.4*scale,z);}
function windows(parent,w,d,level,height=2){for(const side of [-1,1])for(let i=-1;i<=1;i++){
const x=i*w*.3,y=level+height*.54;
box(.39,.73,.07,windowMat,x,y,side*d/2+.04,parent);
box(.42,.14,.09,M.timber,x,y+.44,side*d/2+.065,parent);
}
for(const side of [-1,1])for(let i=-1;i<=1;i++){
let z=i*d*.26,y=level+height*.54;
box(.07,.72,.38,windowMat,side*w/2+.05,y,z,parent);
}}
function pinFlag(parent,x,y,z,size=.82,blue=true){cyl(.055,.055,size*1.9,6,M.timber,x,y+size*.35,z,parent);const f=box(size*.78,size*.45,.045,blue?M.blue:M.red,x+size*.42,y+size*.7,z,parent);f.userData.isFlag=true;flags.push({mesh:f,y:f.position.y,speed:1.5+rng(),phase:rng()*6})}
function roof(parent,w,d,y,material=M.roof,h=1.55){ // six-point gabled prism, ridgeline parallel to z axis
const vertices=[
 -w/2,y,-d/2,w/2,y,-d/2,0,y+h,-d/2,
 -w/2,y,d/2,w/2,y,d/2,0,y+h,d/2
];
const face=[0,2,1,3,4,5,0,3,5,0,5,2,1,2,5,1,5,4,0,1,4,0,4,3];
const pos=[];for(const id of face)pos.push(...vertices.slice(id*3,id*3+3));
let geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));geo.computeVertexNormals();
const r=mesh(geo,material,0,0,0,parent);r.castShadow=true;
box(.1,.15,d+.06,M.gold,0,y+h,z=0,parent);
return r;
}
function house(x,z,w=2.4,d=2.1,color=M.plaster,roofColor=M.roof,rot=0){
const g=new THREE.Group();g.position.set(x,.1,z);g.rotation.y=rot;worldRoot.add(g);
box(w,2.2,d,color,0,1.25,0,g);
for(const v of [-1,1]){box(w+.1,.12,.14,M.timber,0,.35,v*d/2,g);box(.12,2,d+.04,M.timber,v*w/2,1.1,0,g)}
roof(g,w+.35,d+.35,2.35,roofColor,1.1);
windows(g,w,d,.4,1.8);
box(.66,1.23,.07,M.timber,0,.72,d/2+.045,g);
return g;
}
function towerPiece(parent,x,z,h=7,r=1.25,blue=true){
cyl(r,r,h,8,M.wall,x,h/2+.35,z,parent);
cyl(r*1.15,r*1.15,.42,8,M.stoneDark,x,h+.5,z,parent);
for(let k=0;k<8;k++){const a=k*Math.PI/4;box(.5,.45,.48,M.wall,x+Math.sin(a)*r,h+.93,z+Math.cos(a)*r,parent)}
cone(r*1.38,2.5,8,blue?M.blue:M.roofDark,x,h+1.8,z,parent);
cyl(.09,.09,.9,6,M.gold,x,h+3.2,z,parent);
}
function archDoor(g,x,z,w=1.0,y=.85){box(w,1.5,.08,M.timber,x,y,z,g);cyl(w*.5,w*.5,.1,8,M.timber,x,y+.8,z,g).rotation.x=Math.PI/2}
function createBuilding(key,x,z,build,anchorY){
const group=new THREE.Group();group.position.set(x,.12,z);group.userData.buildingKey=key;worldRoot.add(group);build(group);selectable.set(key,group);anchors.set(key,new THREE.Vector3(x,anchorY,z));return group;
}
function hall(g){
box(6.6,4.5,5.3,M.plaster,0,2.65,0,g);box(6.75,.35,5.48,M.stoneDark,0,.45,0,g);roof(g,7.05,5.7,4.9,M.blue,2.5);
for(const x of [-3.55,3.55]){towerPiece(g,x,-2.7,5.1,1.02,true);towerPiece(g,x,2.7,4.1,.9,true);}
box(1.45,2.5,.16,M.timber,0,1.7,2.71,g);
for(let i=-2;i<=2;i++)for(let j=0;j<=1;j++){if(i===0&&j===0)continue;box(.5,.72,.1,windowMat,i*1.05,2.2+j*1.25,2.7,g)}
box(4,.25,1,M.stoneDark,0,.42,3.15,g);box(3,.25,1,M.road,0,.2,3.6,g);
cyl(.3,.34,1,6,M.gold,0,7.75,0,g);pinFlag(g,0,7.9,0,1.15,true);
}
function treasury(g){
box(5.7,3.2,4.8,M.wall,0,1.95,0,g);box(6.0,.3,5.0,M.stoneDark,0,.35,0,g);
for(const x of [-2.5,-1.5,-.5,.5,1.5,2.5]){cyl(.23,.23,2.9,8,M.plaster,x,2.15,2.45,g);}
box(6.5,.3,5.5,M.gold,0,3.65,0,g);cyl(2.05,2.05,.65,12,M.gold,0,4.13,0,g);cone(2.15,2.2,12,M.blue,0,5.4,0,g);
cyl(.27,.27,.65,10,M.gold,0,6.82,0,g);cone(.5,.9,8,M.gold,0,7.2,0,g);
archDoor(g,0,2.48,1.1);for(const x of [-1.8,1.8])box(.59,.87,.10,windowMat,x,2.7,2.46,g);
}
function watchtower(g){
cyl(2.4,2.6,.65,10,M.stoneDark,0,.27,0,g);towerPiece(g,0,0,8.1,1.75,true);
for(const i of [-1,0,1])box(.46,.75,.13,windowMat,i*.7,4.8,1.77,g);
pinFlag(g,0,11.8,0,1.3,true);
}
function guild(g){
box(5.4,3.4,4.3,M.plaster,0,2,0,g);roof(g,5.9,4.6,3.8,M.roof,2.1);
for(const x of [-2.67,2.67]){box(.25,3.8,4.48,M.timber,x,1.95,0,g)}
for(const z of [-2.17,2.17])box(5.5,.21,.16,M.timber,0,2.4,z,g);
windows(g,5.4,4.3,.7,2.4);archDoor(g,0,2.21,1.1);
box(1.2,1.1,.14,M.blue,2.15,4.3,2.3,g);pinFlag(g,-2,5.2,0,.8,true);
}
function library(g){
box(5.2,3.8,4.4,M.roofDark,0,2.1,0,g);box(5.38,.44,4.8,M.wall,0,.42,0,g);roof(g,5.85,4.85,4.1,M.blue,1.8);
for(const x of [-1.8,-.6,.6,1.8]){box(.68,1.8,.11,windowMat,x,2.4,2.28,g);box(.15,1.85,.12,M.gold,x,2.4,2.35,g);}
archDoor(g,0,2.31,1.04);pinFlag(g,-2,6.5,0,.8,false);
}
function gateway(g){
box(8.2,3.4,1.9,M.wall,0,1.86,0,g);
towerPiece(g,-3.7,0,4.35,1.0,true);towerPiece(g,3.7,0,4.35,1.0,true);
box(2.7,2.85,2.1,M.timber,0,1.5,0,g);
box(2.7,.48,2.2,M.stoneDark,0,3.25,0,g);
for(let i=-3;i<=3;i++)box(.65,.48,2.07,M.wall,i*1.08,3.65,0,g);
pinFlag(g,0,5.0,0,.85,true);
}
function fountain(x,z){cyl(2.0,2.0,.3,16,M.stoneDark,x,.42,z);cyl(1.72,1.72,.08,16,mat(0x69aec0),x,.63,z);
cyl(.44,.56,2.1,12,M.stoneDark,x,1.7,z);cyl(.85,.85,.28,12,M.wall,x,2.66,z);cone(.26,.7,8,M.gold,x,3.15,z);}
function stall(x,z,red=false,rot=0){
const g=new THREE.Group();g.position.set(x,.2,z);g.rotation.y=rot;worldRoot.add(g);
for(let a of [-.9,.9])for(let b of [-.6,.6])cyl(.065,.065,2.1,5,M.timber,a,1.15,b,g);
box(2.1,.22,1.5,M.timber,0,.95,0,g);
const tent=cone(1.52,1.15,4,red?M.red:M.blueLight,0,2.4,0,g);tent.rotation.y=Math.PI/4;
for(let j=-1;j<=1;j++)box(.42,.16,.4,[M.gold,M.leaf2,M.red][j+1],j*.56,1.14,0,g);
}
function createWalls(){
const wallH=2.0;const wallZ=13,wallX=20;
for(let x=-20;x<=20;x+=2.2){if(Math.abs(x)<5 && wallZ===13)continue;
for(const z of [-wallZ]){box(2.15,wallH,1.05,M.wall,x,1.12,z);for(const dx of [-.65,.6])box(.55,.44,.99,M.wall,x+dx,2.35,z)}}
for(const z of [-13,13])for(let x=-20;x<=20;x+=2.2){if(z>0&&Math.abs(x)<5)continue;if(z<0&&Math.abs(x)<5)continue;
box(2.12,wallH,1.07,M.wall,x,1.1,z);for(const dx of [-.65,.6])box(.52,.36,1.08,M.wall,x+dx,2.25,z)}
for(const x of [-20,20])for(let z=-11;z<=11;z+=2.2){box(1.15,wallH,2.15,M.wall,x,1.1,z);for(const dz of [-.67,.67])box(1.18,.39,.51,M.wall,x,2.28,z+dz)}
for(const x of [-20,20])for(const z of [-13,13])towerPiece(worldRoot,x,z,2.9,1.25,true);
}
function ship(x,z,rot=0,s=.85){
const b=new THREE.Group();b.position.set(x,.1,z);b.rotation.y=rot;b.scale.setScalar(s);worldRoot.add(b);
const hull=box(3.6,.9,1.65,M.timber,0,.2,0,b);
box(3.2,.1,1.47,M.road,0,.68,0,b);
cyl(.075,.075,5.2,6,M.timber,0,3,0,b);
let sail=mesh(new THREE.PlaneGeometry(2.3,2.9),M.plaster,.96,3.2,0,b);sail.rotation.y=Math.PI/2;sail.castShadow=false;
pinFlag(b,.05,5.4,0,.65,true);boats.push({group:b,initial:.1,phase:rng()*6});
}
function citizen(i){
const person=new THREE.Group(),palette=[0x3c6984,0xc77a48,0xb85d61,0x738358,0xe2b45e,0x8f76aa,0x6d5944];
cyl(.19,.2,.65,7,mat(palette[i%palette.length]),0,.75,0,person);
cyl(.19,.18,.24,8,mat(0xe2bc89),0,1.2,0,person);cone(.2,.24,7,mat(0x554938),0,1.45,0,person);
for(const side of [-1,1])box(.12,.45,.15,M.timber,side*.12,.26,0,person);
worldRoot.add(person);person.scale.setScalar(.8+rng()*.3);
const theta=i/34*Math.PI*2;
const r=5+rng()*7;
const ix=Math.cos(theta)*r,iz=Math.sin(theta)*r*.7;
citizens.push({obj:person,x:ix,z:iz,phi:rng()*Math.PI*2,radius:.7+rng()*2,speed:.12+rng()*.17});
}
function decorations(){
for(let i=0;i<32;i++){let x=(rng()-.5)*36,z=(rng()-.5)*23;if(Math.abs(x)<10&&Math.abs(z)<7)continue;
if(Math.abs(x)<18&&Math.abs(z)<12)tree(x,z,.55+rng()*.45)}
for(let i=0;i<45;i++){let x=(rng()-.5)*36,z=(rng()-.5)*22;if(Math.abs(x)<9&&Math.abs(z)<8)continue;
if(Math.abs(x)<18&&Math.abs(z)<12)shrub(x,z,.14+rng()*.21)}
for(const [x,z,rot] of [[-15,-7,0],[15,6,1],[-14,7,1.1],[14,-7,.2],[-6,-10,.5],[6,-10,-.2]])
house(x,z,2+rng(),1.8+rng()*.7,rng()>.5?M.plaster:M.wall,rng()>.4?M.roof:M.roofDark,rot);
for(let i=0;i<34;i++)citizen(i);
for(let j=0;j<20;j++) {const x=(rng()-.5)*65,z=(rng()-.5)*48;if(Math.abs(x)<23&&Math.abs(z)<16)continue;
const rock=cyl(.8+rng()*.9,1.2+rng()*.7,.8+rng()*.8,6,M.stoneDark,x,.4,z);rock.rotation.y=rng()*3;}
}
function setupLandscape(){
const water=box(150,.45,130,M.water,0,-1.12,0);water.receiveShadow=false;
const island=box(43,1.0,29,M.grass,0,-.39,0);
box(43,.3,29,M.roadBorder,0,-.80,0);
for(let i=0;i<12;i++){let z=(rng()-.5)*24;const x=i%2?-22.5:22.5;cone(1.6,1.9,6,M.leaf2,x,-.1,z)}
path(-16,0,16,0,2.5);path(0,-11,0,11,2.3);path(-12,-7,10,7,1.5);path(-13,6,12,-8,1.2);
plaza(0,3.8,11,8.5);plaza(0,-4.3,9,6);plaza(9.5,-3.6,8,7);plaza(-9,4.5,8,7);
createWalls();
for(let i=0;i<24;i++){const x=(i%2?1:-1)*(22+i%6*1.6),z=(rng()-.5)*30;const seg=box(.14,.022,.4,M.waterLight,x,-.86,z);waves.push({obj:seg,phase:rng()*6})}
}
function setupProps(){
fountain(0,4.7);
for(let i=0;i<10;i++){let angle=(i/10)*Math.PI*2,rad=6.4;stall(Math.sin(angle)*rad,4.8+Math.cos(angle)*rad*.6,i%2===0,angle);}
for(const [x,z] of [[-12,5],[-11,7],[7,6],[12,4],[10,6]]){cyl(.23,.27,.5,7,M.timber,x,.32,z);cyl(.24,.24,.09,8,M.roofDark,x,.59,z)}
for(const p of [[-26,-7,.4],[26,-8,-.3],[-29,9,1.1],[25,10,-1]])ship(...p);
for(let i=0;i<10;i++){const x=i<5?-22.4:22.4,z=(i%5-2)*4.5;box(4,.24,1.15,M.timber,x,-.35,z);for(const dx of [-1.8,1.8])cyl(.14,.14,1.6,6,M.timber,x+dx,-.6,z)}
for(let i=0;i<25;i++){const x=(rng()-.5)*80,z=(rng()-.5)*68;
if(Math.abs(x)<24&&Math.abs(z)<18)continue;tree(x,z,.4+rng()*.85);}
}
function createScene(){
scene=new THREE.Scene();scene.background=new THREE.Color(colors.sky);scene.fog=new THREE.Fog(colors.sky,75,140);
worldRoot=new THREE.Group();scene.add(worldRoot);
scene.add(new THREE.HemisphereLight(0xf5f9dd,0x7394a6,2.05));
const sun=new THREE.DirectionalLight(0xffebc3,2.5);sun.position.set(-20,34,18);sun.castShadow=true;
sun.shadow.mapSize.set(2048,2048);sun.shadow.camera.left=-49;sun.shadow.camera.right=49;sun.shadow.camera.top=40;sun.shadow.camera.bottom=-40;sun.shadow.bias=-.00025;scene.add(sun);
setupLandscape();
createBuilding('townhall',0,-4.8,hall,9.3);
createBuilding('market',0,5.0,g=>{box(2.9,1.5,2.1,M.plaster,0,.88,0,g);roof(g,3.1,2.3,1.63,M.roof,1.1);},4.2);
createBuilding('treasury',10,-4.8,treasury,7.9);
createBuilding('tower',-14,-7,watchtower,12.3);
createBuilding('guild',-10,2.4,guild,6.75);
createBuilding('library',10,3,library,6.9);
createBuilding('gate',0,12.9,gateway,7.2);
decorations();setupProps();
}
function resize(){
if(!renderer||!camera||!container)return;
let w=Math.max(300,container.clientWidth),h=Math.max(250,container.clientHeight),aspect=w/h;
const vertical=(aspect<1.4?36:28)*(h<480?1.16:1);
camera.left=-vertical*aspect/2;camera.right=vertical*aspect/2;camera.top=vertical/2;camera.bottom=-vertical/2;camera.updateProjectionMatrix();
renderer.setSize(w,h,false);
}
function selectBuilding(key,focus=false){
if(!selectable.has(key))return;selected=key;
for(const [name,g] of selectable)g.traverse(x=>{if(x.isMesh&&x.material?.emissive){x.material.emissiveIntensity=x.material.userData?.baseGlow||0}});
const g=selectable.get(key);
tracking.active=focus;tracking.focus=focus?new THREE.Vector3(g.position.x,1,g.position.z):null;
document.querySelectorAll('.place-label').forEach(l=>l.classList.toggle('active',l.dataset.select===key));
}
function updateLabels(){
const rect=container.getBoundingClientRect();
for(const [key,v] of anchors){
const b=document.getElementById('label-'+key);if(!b)continue;
const point=v.clone().project(camera);
const px=(point.x*.5+.5)*rect.width,py=(-point.y*.5+.5)*rect.height;
b.style.left=px+'px';b.style.top=(py-4)+'px';
b.style.visibility=point.z>1||px<-60||px>rect.width+60||py<-60||py>rect.height+60?'hidden':'visible';
}
}
function animate(time){
const dt=Math.min(.06,((time-last)||16)/1000);last=time;
if(run){tick+=dt;for(const c of citizens){c.obj.position.set(c.x+Math.cos(tick*c.speed+c.phi)*c.radius,.2+Math.abs(Math.sin(tick*3+c.phi))*.04,c.z+Math.sin(tick*c.speed+c.phi)*c.radius);
c.obj.rotation.y=-tick*c.speed-c.phi;}
for(const b of boats)b.group.position.y=b.initial+Math.sin(tick*.85+b.phase)*.12;
for(const f of flags)f.mesh.rotation.z=Math.sin(tick*f.speed+f.phase)*.065;
for(const s of waves)s.obj.position.y=-.86+Math.sin(tick*1.6+s.phase)*.032;
}
if(tracking.active&&tracking.focus){
const t=tracking.focus;controls.target.lerp(t,Math.min(1,dt*1.8));
const wished=t.clone().add(new THREE.Vector3(17,21,20));camera.position.lerp(wished,Math.min(1,dt*1.3));
if(camera.position.distanceTo(wished)<.07)tracking.active=false;
}
controls.update();renderer.render(scene,camera);updateLabels();requestAnimationFrame(animate);
}
export function initTown(node,onSelect){
container=node;raycaster=new THREE.Raycaster();mouse=new THREE.Vector2();
renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:'high-performance'});
renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.7));renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.4;
container.appendChild(renderer.domElement);
const aspect=container.clientWidth/Math.max(250,container.clientHeight),vertical=29;
camera=new THREE.OrthographicCamera(-aspect*vertical/2,aspect*vertical/2,vertical/2,-vertical/2,.1,250);
camera.position.set(28,31,33);camera.lookAt(0,0,0);
controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.075;
controls.enablePan=true;controls.screenSpacePanning=true;controls.minZoom=.72;controls.maxZoom=2.5;controls.maxPolarAngle=Math.PI*.48;controls.minPolarAngle=Math.PI*.17;
controls.maxDistance=100;controls.minDistance=12;controls.target.set(0,0,0);
controls.addEventListener('start',()=>{tracking.active=false});
createScene();resize();selectBuilding('townhall');
const observer=new ResizeObserver(resize);observer.observe(container);
let down=null;renderer.domElement.addEventListener('pointerdown',e=>{down={x:e.clientX,y:e.clientY}});
renderer.domElement.addEventListener('pointerup',e=>{
if(!down||Math.hypot(e.clientX-down.x,e.clientY-down.y)>6)return;
const rect=renderer.domElement.getBoundingClientRect();mouse.x=((e.clientX-rect.left)/rect.width)*2-1;mouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
raycaster.setFromCamera(mouse,camera);
let hits=raycaster.intersectObjects([...selectable.values()],true);
if(hits.length){let parent=hits[0].object;while(parent&&!parent.userData?.buildingKey)parent=parent.parent;
if(parent){selectBuilding(parent.userData.buildingKey,true);onSelect(parent.userData.buildingKey)}}
});
requestAnimationFrame(animate);
return {
 select:(key,focus=true)=>selectBuilding(key,focus),
 reset:()=>{tracking.active=false;controls.target.set(0,0,0);camera.position.set(28,31,33);camera.zoom=1;camera.updateProjectionMatrix();controls.update()},
 zoom:(delta)=>{camera.zoom=clip(camera.zoom+delta,.72,2.5);camera.updateProjectionMatrix()},
 animate:(value)=>{run=Boolean(value)}
};
}
