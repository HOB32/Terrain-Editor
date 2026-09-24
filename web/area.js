/* Terrain [beta] · Create area: a new height field to sculpt, paint with the
   zone's own ground types, flood with water, and lay roads and bridges on.
   Saved as an area project under the data root (see terrain_api.Areas). */
'use strict';
(()=>{
const T=window.TerrainCore;if(!T)return;
const {gl,$,api,status,b64}=T;
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),smooth01=x=>{x=clamp(x,0,1);return x*x*(3-2*x);};
const TOOLS=['raise','lower','smooth','flatten','paint','water','road','bridge'];
const BRUSH_TOOLS=new Set(['raise','lower','smooth','flatten','paint','water']);
const HINTS={
 raise:'Hold the left button to raise the ground. Right-drag orbits, Shift+right-drag pans, [ and ] resize the brush.',
 lower:'Hold the left button to dig the ground down. [ and ] resize the brush.',
 smooth:'Brush over bumps and ridges to smooth them out.',
 flatten:'Press where the height is right, then brush: everything under the brush levels to that height.',
 paint:'Pick a ground type, then brush it on. Lower strength gives a ragged, natural edge.',
 water:'Fill paints water at the level below; Dig & fill also carves a bed under it; Remove dries it. Alt+click the ground to take its height as the level.',
 road:'Click along the road, then double-click, press Enter or Finish road. The ground is levelled under it and painted with the surface. Backspace removes the last point.',
 bridge:'Click one bank, then the other. The deck runs between the two clicks, Deck height above them. Esc cancels.'};
const RING={raise:[.45,1,.55,1],lower:[1,.45,.4,1],smooth:[.45,.75,1,1],flatten:[1,.72,.25,1],paint:[1,1,1,1],water:[.3,.85,1,1]};
const QUICK=[['Grass','z_grass'],['Mud','z_dirt_dark'],['Dirt','z_dirt'],['Gravel','z_gravel'],['Rock','z_rock'],['Asphalt','z_asphalt'],['Forest floor','z_pineforest_floor01'],['Puddles','z_puddle']];
const ROAD_SURFACES=['z_asphalt','z_asphalt_worn','z_road_rubble','z_gravel','z_dirt_compacted','z_dirt'];

// ------------------------------------------------------------ state
let tab='explore',area=null,rec=null,tool=null,paintId=null,cursor=null,stroke=null,copying=false,token='';
let history=[],future=[],roadPts=[],bridgeA=null,lastClick=0;
let bridgeSets=[],bridgeMeshes=[],bridgeBuild=0,waterVAO=null,waterBufs=[],waterCount=0;

// ------------------------------------------------------------ programs
const waterProg=T.program(`#version 300 es
in vec3 aPos;in float aDepth;uniform mat4 uVP;out vec3 vP;out float vD;
void main(){vP=aPos;vD=aDepth;gl_Position=uVP*vec4(aPos,1.);}`,`#version 300 es
precision highp float;in vec3 vP;in float vD;uniform vec3 uEye;uniform float uTime,uFog;out vec4 o;
void main(){if(vD<.03)discard;
 vec3 n=normalize(vec3(sin(vP.x*.21+uTime*1.1)*.05+sin((vP.x+vP.z)*.53-uTime*1.7)*.035,1.,cos(vP.z*.27-uTime*.9)*.05+sin((vP.x-vP.z)*.61+uTime*1.3)*.03));
 vec3 v=normalize(uEye-vP),L=normalize(vec3(.45,.8,.35)),h=normalize(L+v);float fr=pow(1.-max(dot(n,v),0.),3.),spec=pow(max(dot(n,h),0.),90.)*1.1;
 vec3 c=mix(vec3(.2,.4,.38),vec3(.03,.12,.17),smoothstep(.3,5.,vD));c=mix(c,vec3(.55,.66,.75),fr*.55)+spec;
 float a=max(mix(.5,.9,smoothstep(.2,4.,vD)),fr*.9);float f=1.-exp(-length(vP-uEye)*uFog);c=mix(c,vec3(.11,.14,.17),clamp(f,0.,.9));
 o=vec4(pow(c,vec3(1./1.1)),a);}`);
const WL=T.loc(waterProg,['aPos','aDepth','uVP','uEye','uTime','uFog']);
const lineProg=T.program(`#version 300 es
in vec3 aPos;uniform mat4 uVP;void main(){gl_Position=uVP*vec4(aPos,1.);}`,`#version 300 es
precision highp float;uniform vec4 uCol;out vec4 o;void main(){o=uCol;}`);
const LL=T.loc(lineProg,['aPos','uVP','uCol']);
const lineVAO=gl.createVertexArray(),lineBuf=gl.createBuffer();
gl.bindVertexArray(lineVAO);gl.bindBuffer(gl.ARRAY_BUFFER,lineBuf);gl.enableVertexAttribArray(LL.aPos);gl.vertexAttribPointer(LL.aPos,3,gl.FLOAT,false,0,0);gl.bindVertexArray(null);
function drawLine(pts,mode,col){if(pts.length<3)return;gl.bindVertexArray(lineVAO);gl.bindBuffer(gl.ARRAY_BUFFER,lineBuf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(pts),gl.DYNAMIC_DRAW);gl.uniform4fv(LL.uCol,col);gl.drawArrays(mode,0,pts.length/3);}

// ------------------------------------------------------------ ground types
function ecoId(name){const p=T.palette();name=name.toLowerCase();for(const [id,e] of Object.entries(p))if(String(e.name).toLowerCase()===name&&+id<32)return +id;return null;}
function ecoName(id){const e=T.palette()[id];if(!e)return `Type ${id}`;const n=String(e.name).toLowerCase();
 if(n==='z_dirt_dark')return 'Mud';if(n==='z_puddle')return 'Puddles';
 const s=String(e.name).replace(/^z_/i,'').replace(/\d+$/,'').replace(/_/g,' ').trim().toLowerCase();return s.charAt(0).toUpperCase()+s.slice(1);}
function defaultGround(){return ecoId('z_grass')??ecoId('z_grass_thick')??0;}
function renderGround(){
 const p=T.palette(),ids=Object.keys(p).map(Number).filter(i=>i<32).sort((a,b)=>a-b);
 if(paintId==null||!(paintId in p))paintId=defaultGround();
 $('quickGround').innerHTML=QUICK.map(([label,n])=>{const id=ecoId(n);return id==null?'':`<button data-eco="${id}" class="${id===paintId?'on':''}">${label}</button>`;}).join('');
 $('groundSwatches').innerHTML=ids.map(id=>`<button class="swatch ${id===paintId?'on':''}" data-eco="${id}" title="${p[id].name}" style="background-image:url('/api/terrain/texture?name=${encodeURIComponent(p[id].texture)}&size=64');background-color:rgb(${p[id].colour.map(v=>Math.round(v*255)).join(',')})">${ecoName(id)}</button>`).join('')||'<div class="legend">The zone\'s ground types are still loading.</div>';
 const surf=ROAD_SURFACES.map(ecoId).filter(v=>v!=null),keep=$('roadSurface').value;
 $('roadSurface').innerHTML=(surf.length?surf:ids).map(id=>`<option value="${id}">${ecoName(id)}</option>`).join('');if(keep)$('roadSurface').value=keep;}
for(const box of ['quickGround','groundSwatches'])$(box).addEventListener('click',e=>{const b=e.target.closest('[data-eco]');if(!b)return;paintId=+b.dataset.eco;renderGround();if(tool!=='paint')setTool('paint');status(`Painting ${ecoName(paintId)}.`);});

// ------------------------------------------------------------ the height field
function blank(size,origin){const res=size<=512?1:2,n=size/res+1;
 return {name:'',zone:T.zone(),origin,size,res,n,heights:new Float32Array(n*n),water:new Float32Array(n*n).fill(NaN),ground:new Uint8Array((size+1)*(size+1)),roads:[],bridges:[]};}
function sample(x,z){const a=area;if(!a)return null;const fx=(x-a.origin[0])/a.res,fz=(z-a.origin[1])/a.res;if(fx<0||fz<0||fx>a.n-1||fz>a.n-1)return null;
 const n=a.n,i=Math.min(n-2,Math.floor(fx)),j=Math.min(n-2,Math.floor(fz)),u=fx-i,v=fz-j,H=a.heights;
 return (H[j*n+i]*(1-u)+H[j*n+i+1]*u)*(1-v)+(H[(j+1)*n+i]*(1-u)+H[(j+1)*n+i+1]*u)*v;}
function hit(o,d){ // march the ray over the height field, then bisect
 const a=area;if(!a)return Infinity;const x0=a.origin[0],z0=a.origin[1];let t0=0,t1=1e7;
 for(const [oo,dd,lo,hi] of [[o[0],d[0],x0,x0+a.size],[o[2],d[2],z0,z0+a.size]]){if(Math.abs(dd)<1e-9){if(oo<lo||oo>hi)return Infinity;continue;}let ta=(lo-oo)/dd,tb=(hi-oo)/dd;if(ta>tb)[ta,tb]=[tb,ta];t0=Math.max(t0,ta);t1=Math.min(t1,tb);}
 if(t0>t1)return Infinity;if(d[1]<0&&rec)t0=Math.max(t0,(rec.max+1-o[1])/d[1]);if(t0>t1)return Infinity;
 const above=t=>{const h=sample(o[0]+d[0]*t,o[2]+d[2]*t);return h==null||o[1]+d[1]*t>h;};
 if(!above(t0))return t0;const step=a.res*.5;let prev=t0;
 for(let t=t0+step;t<=t1+step;t+=step){if(!above(t)){let lo=prev,hi=t;for(let k=0;k<16;k++){const m=(lo+hi)/2;if(above(m))lo=m;else hi=m;}return hi;}prev=t;}
 return Infinity;}
function pick(e){if(!rec)return null;const {o,d}=T.ray(e),t=hit(o,d);return t<Infinity?[o[0]+d[0]*t,o[1]+d[1]*t,o[2]+d[2]*t]:null;}

let nrm=null;
function normals(j0,j1){const a=area,n=a.n,H=a.heights,r2=2*a.res;
 for(let j=Math.max(0,j0);j<=Math.min(n-1,j1);j++)for(let i=0;i<n;i++){const k=j*n+i,nx=H[j*n+Math.max(0,i-1)]-H[j*n+Math.min(n-1,i+1)],nz=H[Math.max(0,j-1)*n+i]-H[Math.min(n-1,j+1)*n+i],l=Math.hypot(nx,r2,nz);nrm[k*3]=nx/l;nrm[k*3+1]=r2/l;nrm[k*3+2]=nz/l;}}
function range(){let lo=Infinity,hi=-Infinity;for(const h of area.heights){if(h<lo)lo=h;if(h>hi)hi=h;}rec.min=lo;rec.max=hi;}
function build(){
 dispose();const a=area,n=a.n,res=a.res,[ox,oz]=a.origin,N=n*n,G=T.G;
 const pos=new Float32Array(N*3);nrm=new Float32Array(N*3);
 for(let j=0;j<n;j++)for(let i=0;i<n;i++){const k=j*n+i;pos[k*3]=ox+i*res;pos[k*3+1]=a.heights[k];pos[k*3+2]=oz+j*res;}
 normals(0,n-1);
 const idx=new Uint32Array((n-1)*(n-1)*6);let q=0;for(let j=0;j<n-1;j++)for(let i=0;i<n-1;i++){const k=j*n+i;idx[q++]=k;idx[q++]=k+n;idx[q++]=k+1;idx[q++]=k+1;idx[q++]=k+n;idx[q++]=k+n+1;}
 const lines=new Uint32Array(n*(n-1)*4);q=0;for(let j=0;j<n;j++)for(let i=0;i<n-1;i++){lines[q++]=j*n+i;lines[q++]=j*n+i+1;}for(let i=0;i<n;i++)for(let j=0;j<n-1;j++){lines[q++]=j*n+i;lines[q++]=(j+1)*n+i;}
 const vao=gl.createVertexArray();gl.bindVertexArray(vao);
 const bufs=[T.buffer(pos),T.buffer(nrm),T.buffer(new Float32Array(N*4))];
 for(const [b,l,s] of [[bufs[0],G.aPos,3],[bufs[1],G.aNrm,3],[bufs[2],G.aCol,4]]){gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.enableVertexAttribArray(l);gl.vertexAttribPointer(l,s,gl.FLOAT,false,0,0);}
 const tri=T.buffer(idx,gl.ELEMENT_ARRAY_BUFFER),wire=T.buffer(lines,gl.ELEMENT_ARRAY_BUFFER);gl.bindVertexArray(null);
 const side=a.size+1,gridTex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,gridTex);gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);
 gl.texImage2D(gl.TEXTURE_2D,0,gl.R8,side,side,0,gl.RED,gl.UNSIGNED_BYTE,a.ground);gl.pixelStorei(gl.UNPACK_ALIGNMENT,4);
 gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.NEAREST);
 rec={name:'area:'+(a.name||'new'),isArea:true,vao,bufs,tri,wire,gridTex,grid:{x:ox,z:oz,side},count:idx.length,wcount:lines.length,min:0,max:0,
  box:[ox,oz,ox+a.size,oz+a.size],info:{vertices:N},pos,props:[],objects:[],alive:true,sample,hit};
 range();waterRebuild();rebuildBridges();}
function dispose(){if(!rec)return;T.chunks.delete(rec.name);gl.deleteVertexArray(rec.vao);for(const b of [...rec.bufs,rec.tri,rec.wire])gl.deleteBuffer(b);gl.deleteTexture(rec.gridTex);
 for(const s of bridgeSets)T.freeSet(s);bridgeSets=[];freeBridgeMeshes();if(waterVAO){gl.deleteVertexArray(waterVAO);waterBufs.forEach(b=>gl.deleteBuffer(b));waterVAO=null;waterBufs=[];waterCount=0;}rec=null;}
function updateHeights(j0,j1){const a=area,n=a.n;j0=Math.max(0,j0);j1=Math.min(n-1,j1);if(j0>j1)return;
 for(let j=j0;j<=j1;j++)for(let i=0;i<n;i++){const k=j*n+i;rec.pos[k*3+1]=a.heights[k];}
 const r0=Math.max(0,j0-1),r1=Math.min(n-1,j1+1);normals(r0,r1);
 gl.bindBuffer(gl.ARRAY_BUFFER,rec.bufs[0]);gl.bufferSubData(gl.ARRAY_BUFFER,j0*n*12,rec.pos,j0*n*3,(j1-j0+1)*n*3);
 gl.bindBuffer(gl.ARRAY_BUFFER,rec.bufs[1]);gl.bufferSubData(gl.ARRAY_BUFFER,r0*n*12,nrm,r0*n*3,(r1-r0+1)*n*3);range();}
function updateGround(r0,r1){const side=area.size+1;r0=Math.max(0,r0);r1=Math.min(side-1,r1);if(r0>r1)return;
 gl.bindTexture(gl.TEXTURE_2D,rec.gridTex);gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);gl.texSubImage2D(gl.TEXTURE_2D,0,0,r0,side,r1-r0+1,gl.RED,gl.UNSIGNED_BYTE,area.ground.subarray(r0*side,(r1+1)*side));gl.pixelStorei(gl.UNPACK_ALIGNMENT,4);}
function refreshAll(){updateHeights(0,area.n-1);updateGround(0,area.size);waterRebuild();rebuildBridges();areaInfo();}

// ------------------------------------------------------------ water
function waterRebuild(){
 if(waterVAO){gl.deleteVertexArray(waterVAO);waterBufs.forEach(b=>gl.deleteBuffer(b));waterVAO=null;waterBufs=[];waterCount=0;}
 const a=area,n=a.n,Wt=a.water,H=a.heights,res=a.res,[ox,oz]=a.origin,map=new Int32Array(n*n).fill(-1),pos=[],dep=[],idx=[];
 const vert=k=>{if(map[k]<0){map[k]=dep.length;const i=k%n,j=(k-i)/n;pos.push(ox+i*res,Wt[k],oz+j*res);dep.push(Wt[k]-H[k]);}return map[k];};
 for(let j=0;j<n-1;j++)for(let i=0;i<n-1;i++){const k=j*n+i;if(Number.isNaN(Wt[k])||Number.isNaN(Wt[k+1])||Number.isNaN(Wt[k+n])||Number.isNaN(Wt[k+n+1]))continue;
  // skip cells fully under the ground: they cannot be seen and cost draws
  if(Wt[k]<=H[k]&&Wt[k+1]<=H[k+1]&&Wt[k+n]<=H[k+n]&&Wt[k+n+1]<=H[k+n+1])continue;
  idx.push(vert(k),vert(k+n),vert(k+1),vert(k+1),vert(k+n),vert(k+n+1));}
 if(!idx.length)return;
 waterVAO=gl.createVertexArray();gl.bindVertexArray(waterVAO);
 const pb=T.buffer(new Float32Array(pos)),db=T.buffer(new Float32Array(dep)),ib=T.buffer(new Uint32Array(idx),gl.ELEMENT_ARRAY_BUFFER);
 gl.bindBuffer(gl.ARRAY_BUFFER,pb);gl.enableVertexAttribArray(WL.aPos);gl.vertexAttribPointer(WL.aPos,3,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ARRAY_BUFFER,db);gl.enableVertexAttribArray(WL.aDepth);gl.vertexAttribPointer(WL.aDepth,1,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,ib);gl.bindVertexArray(null);waterBufs=[pb,db,ib];waterCount=idx.length;}
let waterTimer=0;function waterSoon(){if(waterTimer)return;waterTimer=setTimeout(()=>{waterTimer=0;if(area)waterRebuild();},120);}

// ------------------------------------------------------------ brushes
const cellHash=(i,j)=>(((i*73856093)^(j*19349663))>>>0)%1000/1000;
function applyAt(p,dt){
 const a=area,r=+$('brushSize').value,s=+$('brushStrength').value/100;
 if(tool==='paint'){paintAt(p,r,s);return;}
 const n=a.n,res=a.res,[ox,oz]=a.origin,H=a.heights,Wt=a.water;
 const i0=Math.max(0,Math.floor((p[0]-r-ox)/res)),i1=Math.min(n-1,Math.ceil((p[0]+r-ox)/res)),j0=Math.max(0,Math.floor((p[2]-r-oz)/res)),j1=Math.min(n-1,Math.ceil((p[2]+r-oz)/res));
 if(i0>i1||j0>j1)return;
 const src=tool==='smooth'?H.slice():null,level=+$('waterLevel').value,wm=waterMode();let heights=false,water=false;
 for(let j=j0;j<=j1;j++)for(let i=i0;i<=i1;i++){const d=Math.hypot(ox+i*res-p[0],oz+j*res-p[2]);if(d>r)continue;const f=smooth01(1-d/r),k=j*n+i;
  if(tool==='raise'){H[k]+=s*f*dt*14;heights=true;}
  else if(tool==='lower'){H[k]-=s*f*dt*14;heights=true;}
  else if(tool==='smooth'){let sum=0,c=0;for(let dj=-1;dj<=1;dj++)for(let di=-1;di<=1;di++){const ii=i+di,jj=j+dj;if(ii<0||jj<0||ii>=n||jj>=n)continue;sum+=src[jj*n+ii];c++;}H[k]+=(sum/c-H[k])*Math.min(1,s*f*dt*14);heights=true;}
  else if(tool==='flatten'){H[k]+=(stroke.target-H[k])*Math.min(1,s*f*dt*12);heights=true;}
  else if(tool==='water'){
   if(wm==='remove'){if(!Number.isNaN(Wt[k])){Wt[k]=NaN;water=true;}}
   else{if(Wt[k]!==level){Wt[k]=level;water=true;}
    // dig fully in the middle and less towards the rim, so on a slope the bank
    // runs down to the water instead of standing as a cliff at the brush edge
    if(wm==='dig'){const bed=level-(0.6+3*s)*f,bank=smooth01(f*2.2);if(H[k]>bed&&bank>0){H[k]+=(bed-H[k])*Math.min(1,dt*10)*bank;heights=true;}}}}}
 if(heights){updateHeights(j0,j1);water=true;}
 if(water)waterSoon();
 if(heights||water)stroke.changed=true;}
function paintAt(p,r,s){
 const a=area,side=a.size+1,[ox,oz]=a.origin,G=a.ground,id=paintId,inner=r*(.3+.65*s);
 const i0=Math.max(0,Math.floor(p[0]-r-ox)),i1=Math.min(side-1,Math.ceil(p[0]+r-ox)),j0=Math.max(0,Math.floor(p[2]-r-oz)),j1=Math.min(side-1,Math.ceil(p[2]+r-oz));
 let any=false;
 for(let j=j0;j<=j1;j++)for(let i=i0;i<=i1;i++){const d=Math.hypot(ox+i-p[0],oz+j-p[2]);if(d>r)continue;
  if(d>inner&&cellHash(i,j)>(r-d)/Math.max(.001,r-inner))continue;const k=j*side+i;if(G[k]!==id){G[k]=id;any=true;}}
 if(any){updateGround(j0,j1);stroke.changed=true;}}
function waterMode(){return document.querySelector('#waterMode .on')?.dataset.wm||'fill';}
document.querySelectorAll('#waterMode button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#waterMode button').forEach(o=>o.classList.toggle('on',o===b));});
$('waterFlood').onclick=()=>{if(!area)return;const level=+$('waterLevel').value,before=snapshot();let n=0;
 for(let k=0;k<area.heights.length;k++)if(area.heights[k]<level){area.water[k]=level;n++;}
 if(!n){status(`Nothing in this area is lower than ${level} m.`,true);return;}pushHistory(before);waterRebuild();areaInfo();status(`Flooded ${Math.round(n*area.res*area.res).toLocaleString()} m² below ${level} m.`);};

// ------------------------------------------------------------ roads
function catmull(pts,step){
 if(pts.length<2)return pts.slice();const out=[];
 for(let i=0;i<pts.length-1;i++){const p0=pts[i-1]||pts[i],p1=pts[i],p2=pts[i+1],p3=pts[i+2]||pts[i+1],m=Math.max(1,Math.ceil(Math.hypot(p2[0]-p1[0],p2[1]-p1[1])/step));
  for(let s=0;s<m;s++){const t=s/m,t2=t*t,t3=t2*t;out.push([0,1].map(c=>.5*((2*p1[c])+(-p0[c]+p2[c])*t+(2*p0[c]-5*p1[c]+4*p2[c]-p3[c])*t2+(-p0[c]+3*p1[c]-3*p2[c]+p3[c])*t3)));}}
 out.push(pts[pts.length-1].slice());return out;}
function nearestAlong(path,hs,cells,reach,visit){ // every cell within reach of the path: its distance and the road height there
 const best=new Float32Array(cells.count).fill(Infinity),tgt=new Float32Array(cells.count);let r0=Infinity,r1=-Infinity;
 for(let k=0;k<path.length-1;k++){const [x1,z1]=path[k],[x2,z2]=path[k+1],dx=x2-x1,dz=z2-z1,L2=dx*dx+dz*dz||1e-9;
  const i0=Math.max(0,Math.floor((Math.min(x1,x2)-reach-cells.ox)/cells.step)),i1=Math.min(cells.side-1,Math.ceil((Math.max(x1,x2)+reach-cells.ox)/cells.step));
  const j0=Math.max(0,Math.floor((Math.min(z1,z2)-reach-cells.oz)/cells.step)),j1=Math.min(cells.side-1,Math.ceil((Math.max(z1,z2)+reach-cells.oz)/cells.step));
  for(let j=j0;j<=j1;j++)for(let i=i0;i<=i1;i++){const px=cells.ox+i*cells.step,pz=cells.oz+j*cells.step,u=clamp(((px-x1)*dx+(pz-z1)*dz)/L2,0,1),d=Math.hypot(px-x1-u*dx,pz-z1-u*dz);
   if(d<=reach){const v=j*cells.side+i;if(d<best[v]){best[v]=d;tgt[v]=hs?hs[k]+(hs[k+1]-hs[k])*u:0;}if(j<r0)r0=j;if(j>r1)r1=j;}}}
 for(let v=0;v<best.length;v++)if(best[v]<Infinity)visit(v,best[v],tgt[v]);return [r0,r1];}
function bakeRoad(road,flatten,verge){
 const a=area,path=catmull(road.points,1),half=road.width/2,shoulder=Math.max(4,road.width*.6);
 let hs=path.map(p=>sample(p[0],p[1])??0);   // grade: the ground, averaged along ~25 m so the road runs smoothly
 for(let pass=0;pass<3;pass++){const out=hs.slice(),w=12;for(let k=0;k<hs.length;k++){let s=0,c=0;for(let q=Math.max(0,k-w);q<=Math.min(hs.length-1,k+w);q++){s+=hs[q];c++;}out[k]=s/c;}hs=out;}
 if(flatten){const H=a.heights;const [r0,r1]=nearestAlong(path,hs,{ox:a.origin[0],oz:a.origin[1],step:a.res,side:a.n,count:a.n*a.n},half+shoulder,(v,d,t)=>{
   const w=d<=half+.5?1:1-smooth01((d-half-.5)/(shoulder-.5));H[v]+=(t-H[v])*w;});updateHeights(r0,r1);}
 const G=a.ground,side=a.size+1,vergeId=verge&&verge!=='none'?ecoId(verge):null,edge=vergeId!=null?1.6:0;
 const [g0,g1]=nearestAlong(path,null,{ox:a.origin[0],oz:a.origin[1],step:1,side,count:side*side},half+edge,(v,d)=>{
   if(d<=half)G[v]=road.surface;else if(vergeId!=null&&G[v]!==road.surface&&cellHash(v%side,(v/side)|0)<.85)G[v]=vergeId;});
 updateGround(g0,g1);waterSoon();}
function addRoadPoint(h){const now=performance.now(),last=roadPts[roadPts.length-1];
 if(last&&now-lastClick<380&&Math.hypot(h[0]-last[0],h[2]-last[1])<5){finishRoad();return;}
 lastClick=now;roadPts.push([+h[0].toFixed(2),+h[2].toFixed(2)]);status(`Road: ${roadPts.length} point${roadPts.length===1?'':'s'}. Double-click or Enter to finish.`);}
function finishRoad(){if(roadPts.length<2){status('A road needs at least two points.',true);return;}
 const before=snapshot(),road={points:roadPts,width:+$('roadWidth').value,surface:+$('roadSurface').value};
 bakeRoad(road,$('roadFlatten').checked,$('roadVerge').value);area.roads.push(road);pushHistory(before);roadPts=[];areaInfo();
 status(`Road laid: ${Math.round(pathLength(road.points))} m of ${ecoName(road.surface).toLowerCase()}, ${road.width} m wide.`);}
const pathLength=p=>p.reduce((s,q,i)=>i?s+Math.hypot(q[0]-p[i-1][0],q[1]-p[i-1][1]):0,0);
$('roadFinish').onclick=finishRoad;$('roadBack').onclick=()=>{roadPts.pop();};$('roadCancel').onclick=()=>{roadPts=[];status('Road cancelled.');};

// ------------------------------------------------------------ bridges
// Game bridge pieces, measured from their models: the concrete span is 32 m
// long along -z from its pivot (deck at y 0, its own pier below); a Bailey
// section is 12 m centred on its pivot, with a ramp that attaches at z +6.
const CONCRETE={deck:'Structure_BridgesConcrete01.adr',length:32};
const BAILEY={deck:'Common_Structures_Bridges_BaileyBridgeFlat_RM.adr',ramp:'Common_Structures_Bridges_BaileyBridgeRamp_RM.adr',support:'Common_Structures_Bridges_BaileyBridgeSupport_RM.adr',length:12};
function bridgePieces(br){
 const [ax,ay,az]=br.a,[bx,by,bz]=br.b,dx=bx-ax,dy=by-ay,dz=bz-az,h=Math.hypot(dx,dz),ux=dx/h,uz=dz/h,rows=[],at=t=>[ax+dx*t,ay+dy*t,az+dz*t];
 if(br.style==='concrete'){const n=Math.max(1,Math.round(h/CONCRETE.length)),sz=h/n/CONCRETE.length,yaw=Math.atan2(-dx,-dz),pitch=Math.atan2(dy,h);
  for(let k=0;k<n;k++)rows.push({actor:CONCRETE.deck,row:[...at(k/n),yaw,pitch,0,1,1,sz]});}
 else if(br.style==='bailey'){const n=Math.max(1,Math.round(h/BAILEY.length)),sz=h/n/BAILEY.length,yaw=Math.atan2(dx,dz),pitch=-Math.atan2(dy,h);
  for(let k=0;k<n;k++)rows.push({actor:BAILEY.deck,row:[...at((k+.5)/n),yaw,pitch,0,1,1,sz]});
  rows.push({actor:BAILEY.ramp,row:[bx-ux*6,by,bz-uz*6,yaw,0,0,1,1,1]},{actor:BAILEY.ramp,row:[ax+ux*6,ay,az+uz*6,yaw+Math.PI,0,0,1,1,1]});
  for(let k=1;k<n;k++){const p=at(k/n),g=sample(p[0],p[2]);if(g!=null&&p[1]-g>2.5)rows.push({actor:BAILEY.support,row:[...p,yaw,0,0,1,1,1]});}}
 return rows;}
function cuboid(c){return {v:c,f:[[0,1,2],[0,2,3],[4,6,5],[4,7,6],[0,4,5],[0,5,1],[1,5,6],[1,6,2],[2,6,7],[2,7,3],[3,7,4],[3,4,0]]};}
function simpleBridge(br){ // a concrete deck with kerbs and square piers down to the ground
 const [ax,ay,az]=br.a,[bx,by,bz]=br.b,dx=bx-ax,dz=bz-az,h=Math.hypot(dx,dz),ux=dx/h,uz=dz/h,px=-uz,pz=ux,w=br.width/2,parts=[];
 const slab=(o1,o2,y1a,y1b,y2a,y2b)=>{const A=[ax+px*o1,az+pz*o1],B=[ax+px*o2,az+pz*o2];
  return cuboid([[A[0],y1a,A[1]],[B[0],y1a,B[1]],[B[0]+dx,y1b,B[1]+dz],[A[0]+dx,y1b,A[1]+dz],[A[0],y2a,A[1]],[B[0],y2a,B[1]],[B[0]+dx,y2b,B[1]+dz],[A[0]+dx,y2b,A[1]+dz]]);};
 parts.push(slab(-w,w,ay-.8,by-.8,ay,by),slab(-w,-w+.35,ay,by,ay+.9,by+.9),slab(w-.35,w,ay,by,ay+.9,by+.9));
 const piers=Math.max(0,Math.floor(h/12));
 for(let k=1;k<=piers;k++){const t=k/(piers+1),x=ax+dx*t,z=az+dz*t,y=ay+(by-ay)*t-.8,g=sample(x,z);if(g==null||y-g<1)continue;
  for(const o of [-w*.6,w*.6]){const cx=x+px*o,cz=z+pz*o,s=.45;parts.push(cuboid([[cx-s,g-1,cz-s],[cx+s,g-1,cz-s],[cx+s,g-1,cz+s],[cx-s,g-1,cz+s],[cx-s,y,cz-s],[cx+s,y,cz-s],[cx+s,y,cz+s],[cx-s,y,cz+s]]));}}
 return parts;}
function freeBridgeMeshes(){for(const m of bridgeMeshes){m.bufs.forEach(b=>gl.deleteBuffer(b));gl.deleteBuffer(m.ibuf);}bridgeMeshes=[];}
async function rebuildBridges(){
 const n=++bridgeBuild,groups=new Map(),sets=[],meshes=[];
 for(const br of area?area.bridges:[]){if(br.style==='simple'){const m=T.meshFrom(simpleBridge(br),[.58,.57,.54]);meshes.push(m);const s=T.makeSet(m,[[0,0,0,0,0,0,1,1,1]]);if(s)sets.push(s);continue;}
  for(const p of bridgePieces(br)){if(!groups.has(p.actor))groups.set(p.actor,[]);groups.get(p.actor).push(p.row);}}
 for(const [actor,rows] of groups){try{const g=await T.geometry(actor);const s=T.makeSet(g,rows);if(s)sets.push(s);}catch(err){status(`${actor}: ${err.message}`,true);}}
 if(n!==bridgeBuild){sets.forEach(T.freeSet);meshes.forEach(m=>{m.bufs.forEach(b=>gl.deleteBuffer(b));gl.deleteBuffer(m.ibuf);});return;}
 for(const s of bridgeSets)T.freeSet(s);freeBridgeMeshes();bridgeSets=sets;bridgeMeshes=meshes;}
function bridgeObjects(){const out=[];for(const br of area.bridges)if(br.style!=='simple')for(const p of bridgePieces(br)){const r=p.row;out.push({actor:p.actor,pos:r.slice(0,3),rot:r.slice(3,6),scale:r.slice(6,9)});}return out;}
function bridgeClick(h){
 if(!bridgeA){bridgeA=h.slice();status('Bridge: now click the other bank.');return;}
 const a=bridgeA,raise=+$('bridgeRaise').value||0;bridgeA=null;
 if(Math.hypot(h[0]-a[0],h[2]-a[2])<6){status('The two ends are too close for a bridge.',true);return;}
 const r3=v=>+v.toFixed(3),br={a:[a[0],a[1]+raise,a[2]].map(r3),b:[h[0],h[1]+raise,h[2]].map(r3),style:$('bridgeStyle').value,width:+$('bridgeWidth').value};
 const before=snapshot();area.bridges.push(br);pushHistory(before);rebuildBridges();areaInfo();
 status(`Bridge built: ${Math.round(Math.hypot(br.b[0]-br.a[0],br.b[2]-br.a[2]))} m, ${$('bridgeStyle').selectedOptions[0].text.toLowerCase()}.`);}
$('bridgeStyle').onchange=()=>{$('bridgeWidth').closest('label').hidden=$('bridgeStyle').value!=='simple';};

// ------------------------------------------------------------ history
function snapshot(){return {h:area.heights.slice(),g:area.ground.slice(),w:area.water.slice(),roads:JSON.stringify(area.roads),bridges:JSON.stringify(area.bridges)};}
function pushHistory(s){history.push(s);if(history.length>20)history.shift();future=[];undoState();}
function restore(s){area.heights.set(s.h);area.ground.set(s.g);area.water.set(s.w);area.roads=JSON.parse(s.roads);area.bridges=JSON.parse(s.bridges);refreshAll();}
function undo(){if(!history.length)return;future.push(snapshot());restore(history.pop());undoState();status('Undone.');}
function redo(){if(!future.length)return;history.push(snapshot());restore(future.pop());undoState();status('Redone.');}
function undoState(){$('areaUndo').disabled=!history.length;$('areaRedo').disabled=!future.length;}
$('areaUndo').onclick=undo;$('areaRedo').onclick=redo;

// ------------------------------------------------------------ tools and input
function setTool(t){tool=tool===t?null:t;roadPts=[];bridgeA=null;
 document.querySelectorAll('[data-tool]').forEach(b=>b.classList.toggle('on',b.dataset.tool===tool));
 $('optBrush').hidden=!BRUSH_TOOLS.has(tool);$('optPaint').hidden=tool!=='paint';$('optWater').hidden=tool!=='water';$('optRoad').hidden=tool!=='road';$('optBridge').hidden=tool!=='bridge';
 $('toolHint').textContent=HINTS[tool]||'Pick a tool. Keys 1 to 8 pick them too.';
 $('hint').innerHTML=tool?'<kbd>Left</kbd> use tool <kbd>Right-drag</kbd> orbit <kbd>Shift</kbd>+<kbd>Right</kbd> pan <kbd>Wheel</kbd> zoom <kbd>W A S D</kbd> fly <kbd>[ ]</kbd> brush size':'<kbd>Drag</kbd> orbit <kbd>Right</kbd> pan <kbd>Wheel</kbd> zoom <kbd>W A S D</kbd> fly <kbd>Shift</kbd> faster <kbd>F</kbd> frame';
 if(tool)T.setMode(null);if(tool==='paint')renderGround();$('bridgeStyle').onchange();}
document.querySelectorAll('[data-tool]').forEach(b=>b.onclick=()=>{if(!area){status('Create or open an area first.',true);return;}setTool(b.dataset.tool);});
for(const [id,unit] of [['brushSize',' m'],['brushStrength','%'],['roadWidth',' m'],['bridgeWidth',' m']]){const out=$(id+'Out'),upd=()=>{out.textContent=$(id).value+unit;};$(id).oninput=upd;upd();}
T.hooks.active=()=>tab==='create'&&!!area&&!!tool;
T.hooks.toolOff=()=>{if(tool)setTool(tool);};
T.hooks.frozen=()=>copying||(tab==='create'&&!!area);
T.hooks.down=e=>{
 if(tab!=='create'||!area||!tool||e.button!==0||e.shiftKey)return false;
 const h=pick(e);if(!h){status('Point at the area.',true);return true;}
 if(tool==='road'){addRoadPoint(h);return true;}
 if(tool==='bridge'){bridgeClick(h);return true;}
 if(tool==='water'&&e.altKey){$('waterLevel').value=(Math.round((h[1]+.5)*2)/2).toFixed(1);status(`Water level set to ${$('waterLevel').value} m.`);return true;}
 if(tool==='paint'&&paintId==null)renderGround();
 stroke={before:snapshot(),target:h[1],last:e,changed:false,lastPoint:h};applyAt(h,1/30);
 stroke.timer=setInterval(()=>{if(!stroke||tool==='paint')return;const q=pick(stroke.last);if(q)applyAt(q,1/30);},33);return true;};
T.hooks.move=(e)=>{
 if(tab!=='create'||!area)return false;cursor=tool?pick(e):null;
 if(!stroke)return false;stroke.last=e;
 if(cursor&&(tool==='paint'||tool==='water')){ // fill the gap between two moves so fast strokes stay unbroken
  const a=stroke.lastPoint,r=+$('brushSize').value,steps=Math.max(1,Math.ceil(Math.hypot(cursor[0]-a[0],cursor[2]-a[2])/Math.max(.5,r/3)));
  for(let k=1;k<=steps;k++){const t=k/steps;applyAt([a[0]+(cursor[0]-a[0])*t,0,a[2]+(cursor[2]-a[2])*t],1/60);}stroke.lastPoint=cursor;}
 return true;};
T.hooks.up=()=>{if(!stroke)return false;clearInterval(stroke.timer);if(stroke.changed){pushHistory(stroke.before);areaInfo();}stroke=null;waterRebuild();return true;};
T.hooks.key=e=>{
 if(tab!=='create'||!area)return false;const k=e.key.toLowerCase(),builder=T.builderOpen()&&T.mode();
 if((e.ctrlKey||e.metaKey)&&!builder){if(k==='z'){e.preventDefault();e.shiftKey?redo():undo();return true;}if(k==='y'){e.preventDefault();redo();return true;}return false;}
 if(!builder&&/^[1-8]$/.test(k)){setTool(TOOLS[+k-1]);return true;}
 if(BRUSH_TOOLS.has(tool)&&(k==='['||k===']')){const s=$('brushSize');s.value=clamp(+s.value*(k===']'?1.2:1/1.2),+s.min,+s.max);s.oninput();return true;}
 if(tool==='road'){if(k==='enter'){finishRoad();return true;}if(k==='backspace'){roadPts.pop();e.preventDefault();return true;}if(k==='escape'){roadPts=[];status('Road cancelled.');return true;}}
 if(tool==='bridge'&&k==='escape'&&bridgeA){bridgeA=null;status('Bridge cancelled.');return true;}
 if(k==='escape'&&tool){setTool(tool);return true;}
 return false;};
T.hooks.draw=(m,e,o)=>{
 if(tab!=='create'||!area||!rec)return;
 for(const s of bridgeSets)T.drawSet(s,o.texOn,o.size);
 if(waterCount){gl.useProgram(waterProg);gl.uniformMatrix4fv(WL.uVP,false,m);gl.uniform3fv(WL.uEye,e);gl.uniform1f(WL.uTime,o.ts/1000);gl.uniform1f(WL.uFog,o.fog);
  gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.depthMask(false);gl.bindVertexArray(waterVAO);gl.drawElements(gl.TRIANGLES,waterCount,gl.UNSIGNED_INT,0);gl.depthMask(true);gl.disable(gl.BLEND);}
 gl.useProgram(lineProg);gl.uniformMatrix4fv(LL.uVP,false,m);gl.disable(gl.DEPTH_TEST);
 const y=(x,z,fall)=>(sample(x,z)??fall)+.35;
 if(cursor&&BRUSH_TOOLS.has(tool)){const r=+$('brushSize').value,pts=[];for(let k=0;k<72;k++){const t=k/72*Math.PI*2,x=cursor[0]+Math.cos(t)*r,z=cursor[2]+Math.sin(t)*r;pts.push(x,y(x,z,cursor[1]),z);}drawLine(pts,gl.LINE_LOOP,RING[tool]);
  drawLine([cursor[0],cursor[1]+.2,cursor[2],cursor[0],cursor[1]+2.5,cursor[2]],gl.LINES,RING[tool]);}
 if(tool==='road'&&(roadPts.length||cursor)){const pts=[...roadPts.map(p=>[p[0],p[1]]),...(cursor?[[cursor[0],cursor[2]]]:[])],line=catmull(pts,2),w=+$('roadWidth').value/2,c=[],l=[],r=[];
  line.forEach((p,i)=>{const q=line[Math.min(line.length-1,i+1)],b=line[Math.max(0,i-1)],dx=q[0]-b[0],dz=q[1]-b[1],len=Math.hypot(dx,dz)||1,nx=-dz/len*w,nz=dx/len*w;c.push(p[0],y(p[0],p[1],0),p[1]);l.push(p[0]+nx,y(p[0]+nx,p[1]+nz,0),p[1]+nz);r.push(p[0]-nx,y(p[0]-nx,p[1]-nz,0),p[1]-nz);});
  drawLine(c,gl.LINE_STRIP,[1,.72,.25,1]);drawLine(l,gl.LINE_STRIP,[1,.72,.25,.6]);drawLine(r,gl.LINE_STRIP,[1,.72,.25,.6]);
  for(const p of roadPts){const h=y(p[0],p[1],0);drawLine([p[0],h,p[1],p[0],h+3,p[1]],gl.LINES,[1,1,1,1]);}}
 if(tool==='bridge'&&bridgeA){const raise=+$('bridgeRaise').value||0,b=cursor||bridgeA;drawLine([bridgeA[0],bridgeA[1]+raise,bridgeA[2],b[0],b[1]+raise,b[2]],gl.LINES,[.4,.85,1,1]);
  drawLine([bridgeA[0],bridgeA[1],bridgeA[2],bridgeA[0],bridgeA[1]+raise+3,bridgeA[2]],gl.LINES,[1,1,1,1]);}
 gl.enable(gl.DEPTH_TEST);gl.useProgram(null);};
T.hooks.map=(mc,mapXY)=>{
 if(tab!=='create')return;let x0,z0,s;
 if(area){[x0,z0]=area.origin;s=area.size;}else{s=+$('areaSize').value;x0=T.cam.target[0]-s/2;z0=T.cam.target[2]-s/2;}
 const [a,b]=mapXY(x0,z0),[c,d]=mapXY(x0+s,z0+s);mc.save();mc.setLineDash(area?[]:[4,3]);mc.strokeStyle='#f5a524';mc.lineWidth=2;mc.strokeRect(a,b,c-a,d-b);
 if(area){mc.fillStyle='rgba(245,165,36,.16)';mc.fillRect(a,b,c-a,d-b);}mc.restore();};

// ------------------------------------------------------------ making, opening, saving
async function copyZone(a){
 const ov=T.overview();if(!ov)throw Error('The zone map has not loaded yet.');
 const [x0,z0]=a.origin,x1=x0+a.size,z1=z0+a.size,need=ov.chunks.filter(c=>c.x<x1&&c.x+c.size>x0&&c.z<z1&&c.z+c.size>z0).map(c=>c.name);
 T.empty(false);T.enqueue(need);const started=Date.now();
 for(;;){const done=need.filter(n=>T.chunks.has(n)||T.state.get(n)==='error').length;status(`Copying the map: loading ground ${done} of ${need.length}…`);
  if(done===need.length)break;if(Date.now()-started>240000)throw Error('Timed out loading the ground to copy.');
  T.enqueue(need.filter(n=>!T.chunks.has(n)&&!T.state.has(n)));await new Promise(r=>setTimeout(r,300));}
 status('Copying the map: sampling heights and ground types…');await new Promise(r=>setTimeout(r,20));
 const g=defaultGround();let sum=0,known=0;
 for(let j=0;j<a.n;j++)for(let i=0;i<a.n;i++){const s=T.zoneSample(x0+i*a.res,z0+j*a.res),k=j*a.n+i;a.heights[k]=s?s.h:NaN;if(s){sum+=s.h;known++;}}
 if(!known)throw Error('No ground here to copy. Fly over the map first.');
 const mean=sum/known;for(let k=0;k<a.heights.length;k++)if(Number.isNaN(a.heights[k]))a.heights[k]=mean;
 const side=a.size+1;for(let j=0;j<side;j++)for(let i=0;i<side;i++){const s=T.zoneSample(x0+i,z0+j);a.ground[j*side+i]=s&&s.eco!=null&&s.eco<32?s.eco:g;}
 return known/(a.n*a.n);}
function enterArea(){
 build();T.unloadAll();T.chunks.set(rec.name,rec);T.empty(false);
 const a=area,cx=a.origin[0]+a.size/2,cz=a.origin[1]+a.size/2;T.cam.target=[cx,sample(cx,cz)??0,cz];T.cam.dist=a.size*1.1;T.cam.pitch=.7;
 history=[];future=[];undoState();$('areaTools').hidden=false;$('areaName').value=a.name||$('areaName').value;renderGround();areaInfo();T.info();T.drawMap();
 if(!tool)setTool('raise');}
$('areaCreate').onclick=async()=>{
 if(area&&!confirm('Replace the open area? Changes since the last save are lost.'))return;
 const size=+$('areaSize').value,t=T.cam.target,origin=[Math.round(t[0]-size/2),Math.round(t[2]-size/2)],a=blank(size,origin),base=Math.round(T.heightAt(t[0],t[2])??t[1]??0);
 a.name=$('areaName').value.trim();a.ground.fill(defaultGround());a.heights.fill(base);
 $('areaCreate').disabled=true;
 try{if($('areaStart').value==='copy'){copying=true;const share=await copyZone(a);if(share<.98)status(`Copied; ${Math.round((1-share)*100)}% of the area had no ground in the zone and was filled flat.`);}
  if($('areaStart').value==='copy'){a.baseline=a.heights.slice();a.groundBase=a.ground.slice();}
  a.game=T.game();if(area)dispose();area=a;$('waterLevel').value=(Math.round(Math.min(...[a.heights[0],a.heights[a.heights.length-1],base])-1)).toFixed(1);enterArea();
  status(`New ${size} m area at x ${origin[0]}, z ${origin[1]}${$('areaStart').value==='copy'?', copied from the map':''}. Sculpt, paint, add water, roads and bridges, then Save area.`);}
 catch(err){status(err.message,true);}
 finally{copying=false;$('areaCreate').disabled=false;}};
function areaInfo(){if(!area){$('areaInfo').textContent='';return;}let wet=0;for(const w of area.water)if(!Number.isNaN(w))wet++;
 $('areaInfo').innerHTML=`<b>${area.name||'Unsaved area'}</b> · ${area.size} m square, heights every ${area.res} m<br>x ${area.origin[0]} to ${area.origin[0]+area.size} · z ${area.origin[1]} to ${area.origin[1]+area.size}<br>ground ${rec?rec.min.toFixed(1):'?'} to ${rec?rec.max.toFixed(1):'?'} m · ${area.roads.length} road${area.roads.length===1?'':'s'} · ${area.bridges.length} bridge${area.bridges.length===1?'':'s'} · ${Math.round(wet*area.res*area.res).toLocaleString()} m² water`;}
function enc(typed){const u=new Uint8Array(typed.buffer,typed.byteOffset,typed.byteLength);let s='';for(let i=0;i<u.length;i+=0x8000)s+=String.fromCharCode.apply(null,u.subarray(i,i+0x8000));return btoa(s);}
async function refreshAreas(){try{const r=await api('/api/terrain/areas');token=r.token;const keep=$('areaList').value;
 $('areaList').innerHTML='<option value="">Open a saved area…</option>'+r.areas.map(x=>`<option value="${x.name}">${x.name} · ${x.zone} · ${x.size} m · ${x.roads} roads · ${x.bridges} bridges</option>`).join('');$('areaList').value=keep;}catch(err){status(err.message,true);}}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json','X-Studio-Token':token},body:JSON.stringify(body)});const d=await r.json().catch(()=>({error:`Unreadable response (${r.status})`}));if(!r.ok)throw Error(d.error||r.statusText);return d;}
async function saveArea(){if(!area)return false;const name=$('areaName').value.trim();if(!name){status('Name the area first.',true);$('areaName').focus();return false;}
 if(!token)await refreshAreas();$('areaSave').disabled=true;status('Saving…');
 try{const d=await post('/api/terrain/areas',{name,zone:area.zone,origin:area.origin,size:area.size,res:area.res,n:area.n,game:area.game||T.game(),heights:enc(area.heights),water:enc(area.water),ground:enc(area.ground),...(area.baseline?{baseline:enc(area.baseline),groundBase:enc(area.groundBase)}:{}),roads:area.roads,bridges:area.bridges,objects:bridgeObjects()});
  area.name=d.name;areaInfo();refreshAreas();status(`Saved ${d.name} to ${d.saved}`);return true;}
 catch(err){status('Save failed: '+err.message,true);return false;}finally{$('areaSave').disabled=false;}}
$('areaSave').onclick=saveArea;
$('areaList').onchange=async()=>{const name=$('areaList').value;if(!name)return;if(area&&!confirm('Open another area? Changes since the last save are lost.')){$('areaList').value='';return;}
 try{status(`Opening ${name}…`);const r=await api('/api/terrain/area?name='+encodeURIComponent(name));
  if((r.game||'steam')!==T.game())await T.setGame(r.game||'steam',r.zone);
  if(r.zone!==T.zone()){$('zone').value=r.zone;await T.openZone(r.zone);}
  if(area)dispose();area={name:r.name,zone:r.zone,origin:r.origin,size:r.size,res:r.res,n:r.n,heights:b64(r.heights,Float32Array),water:b64(r.water,Float32Array),ground:b64(r.ground,Uint8Array),roads:r.roads||[],bridges:r.bridges||[],game:r.game||'steam',
   ...(r.baseline?{baseline:b64(r.baseline,Float32Array),groundBase:b64(r.groundBase,Uint8Array)}:{})};
  $('areaName').value=r.name;enterArea();status(`Opened ${r.name}.`);}catch(err){status(err.message,true);}finally{$('areaList').value='';}};
function closeArea(){dispose();area=null;tool&&setTool(tool);$('areaTools').hidden=true;areaInfo();T.info();T.drawMap();}
$('areaClose').onclick=()=>{if(area&&!confirm('Close this area? Changes since the last save are lost.'))return;closeArea();status('Area closed. The zone loads around the view again.');};
$('areaDelete').onclick=async()=>{const name=(area&&area.name)||$('areaName').value.trim();if(!name||!confirm(`Delete the saved area "${name}"?`))return;
 try{if(!token)await refreshAreas();await post('/api/terrain/areas/delete',{name});closeArea();refreshAreas();status(`Deleted ${name}.`);}catch(err){status(err.message,true);}};
function download(name,blob){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),4000);}
$('exportHeight').onclick=()=>{if(!area)return;const lo=rec.min,hi=rec.max,span=Math.max(.001,hi-lo),out=new Uint16Array(area.heights.length);
 for(let k=0;k<out.length;k++)out[k]=Math.round((area.heights[k]-lo)/span*65535);
 download(`${area.name||'area'}_${area.n}x${area.n}_${area.res}m_${lo.toFixed(2)}to${hi.toFixed(2)}m.raw`,new Blob([out.buffer]));status(`Heightmap: ${area.n}×${area.n} 16-bit, ${lo.toFixed(2)} m = 0 and ${hi.toFixed(2)} m = 65535.`);};
$('exportGround').onclick=()=>{if(!area)return;const side=area.size+1,c=document.createElement('canvas');c.width=c.height=side;const x=c.getContext('2d'),img=x.createImageData(side,side),p=T.palette();
 for(let k=0;k<area.ground.length;k++){const e=p[area.ground[k]],col=e?e.colour:[1,0,1];img.data.set([col[0]*255,col[1]*255,col[2]*255,255],k*4);}
 x.putImageData(img,0,0);c.toBlob(b=>download(`${area.name||'area'}_ground_${side}px.png`,b));status('Ground map: one pixel per metre, coloured by ground type.');};

// ------------------------------------------------------------ build into the game
async function buildZone(){
 if(!area)return;const zoneName=$('buildZone').value.trim();
 if(!/^[A-Za-z][A-Za-z0-9]{2,23}$/.test(zoneName)){status('Zone name: 3-24 letters and digits, starting with a letter.',true);$('buildZone').focus();return;}
 if((area.game||T.game())!=='kotk'||area.zone!=='Z2'){status('Build needs an area made on the KotK depot\'s Z2: pick "KotK depot" under Game on the Explore tab, fly there, and create the area.',true);return;}
 $('buildGo').disabled=true;status(`Building ${zoneName}… the ground under your area is decoded and rewritten, which takes a few seconds a chunk.`);
 try{if(!await saveArea())return;
  const d=await post('/api/terrain/areas/build',{name:area.name,zone:zoneName,install:$('buildInstall').checked});
  const w=d.report.window,env=Object.entries(d.launch.env).map(([k,v])=>`${k}=${v}`).join(' ');
  $('buildInfo').innerHTML=`<b>${d.zone}</b> built: ${d.report.files} files, ${(d.bytes/1048576).toFixed(1)} MB · ${d.report.rewritten.length} ground chunk${d.report.rewritten.length===1?'':'s'} rewritten, ${d.report.copied} copied from Z2${d.report.missing?`, ${d.report.missing} not in Z2`:''}.<br>`+
   (d.installed_now?`Installed as <b>${d.pack}</b> in the KotK client.`:`Not installed; the pack is at ${d.built}.`)+
   `<br>Run the local server with <code>${env}</code> and point the menu camera at x ${d.launch.camera[0]}, z ${d.launch.camera[2]} (menu_views.json).`;
  status(`Zone ${d.zone} built${d.installed_now?' and installed':''}.`);}
 catch(err){status('Build failed: '+err.message,true);}finally{$('buildGo').disabled=false;}}
$('buildGo').onclick=buildZone;
$('buildRemove').onclick=async()=>{const zoneName=$('buildZone').value.trim();if(!zoneName)return;
 try{if(!token)await refreshAreas();const d=await post('/api/terrain/builds/uninstall',{zone:zoneName});status(d.removed?`Removed ${d.removed}.`:`${zoneName} was not installed.`);}catch(err){status(err.message,true);}};

// ------------------------------------------------------------ tabs
function setTab(t){tab=t;const create=t==='create';
 $('tabExplore').hidden=create;$('tabCreate').hidden=!create;$('exploreNote').hidden=create;
 $('tabExploreBtn').classList.toggle('on',!create);$('tabCreateBtn').classList.toggle('on',create);
 $('tabExploreBtn').setAttribute('aria-selected',String(!create));$('tabCreateBtn').setAttribute('aria-selected',String(create));
 const map=$('map');(create?$('areaMapSlot'):$('zone').closest('label')).after(map);
 if(create){refreshAreas();renderGround();if(area&&rec){T.unloadAll();T.chunks.set(rec.name,rec);T.empty(false);}}
 else{if(tool)setTool(tool);cursor=null;if(rec)T.chunks.delete(rec.name);}
 T.info();T.drawMap();}
$('tabExploreBtn').onclick=()=>setTab('explore');$('tabCreateBtn').onclick=()=>setTab('create');
undoState();$('bridgeStyle').onchange();
})();
