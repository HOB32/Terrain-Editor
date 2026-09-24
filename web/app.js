/* Terrain [beta]: fly over a zone with its real ground, buildings and props,
   and build rooms for the server to spawn players into. */
'use strict';
(()=>{
const $=id=>document.getElementById(id);
const api=async p=>{const r=await fetch(p);let d;try{d=await r.json();}catch(e){throw Error(`The server returned an unreadable response (${r.status})`);}if(!r.ok)throw Error(d.error||r.statusText);return d;};
const status=(t,err)=>{$('status').textContent=t;$('status').classList.toggle('err',!!err);};
const b64=(s,T)=>{const bin=atob(s),u=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);return new T(u.buffer);};
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2],norm=a=>{const l=Math.hypot(...a)||1;return a.map(x=>x/l);};
const CHUNK=256;

// ------------------------------------------------------------ state
let zone='Z1',overview=null,hover=null,palette=null,paletteLoad=null,ecoInfo={};
const hooks={};   // filled in by the Create tab (terrain-area.js)
const chunks=new Map();       // name -> loaded chunk
const state=new Map();        // name -> 'loading' | 'error'
let queue=[],working=false,generation=0;

// ------------------------------------------------------------ WebGL
const cv=$('gl'),gl=cv.getContext('webgl2',{antialias:true});
if(!gl){status('WebGL 2 is not available in this browser.',true);return;}
function program(vs,fs){const p=gl.createProgram();for(const [t,s] of [[gl.VERTEX_SHADER,vs],[gl.FRAGMENT_SHADER,fs]]){const sh=gl.createShader(t);gl.shaderSource(sh,s);gl.compileShader(sh);if(!gl.getShaderParameter(sh,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(sh));gl.attachShader(p,sh);}gl.linkProgram(p);if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p));return p;}
// Christmas preview (terrain-season.js): snow on everything facing up, thawed round lit campfires.
const SNOW=`uniform float uSnow;uniform int uMeltN;uniform vec4 uMelt[24];
vec3 snowy(vec3 c,vec3 n,vec3 p,float side){if(uSnow<=0.)return c;float up=smoothstep(.45,.8,n.y),m=1.;
 for(int i=0;i<24;i++){if(i>=uMeltN)break;vec4 q=uMelt[i];m=min(m,smoothstep(q.w*.55,q.w,length(p.xz-q.xz)));}
 float lum=dot(c,vec3(.3,.59,.11));vec3 s=vec3(.9,.93,.98)*(.8+.4*lum);return mix(c,s,uSnow*m*max(up,side));}`;
const LIGHT=`vec3 shade(vec3 base,vec3 n,vec3 p){vec3 L=normalize(vec3(.45,.8,.35));float d=max(dot(n,L),0.);vec3 col=base*(.36+.78*d)+base*vec3(.05,.06,.08)*max(n.y,0.);
 float f=1.-exp(-length(p-uEye)*uFog);return mix(col,vec3(.11,.14,.17),clamp(f,0.,.9));}`;
// Ground: the chunk's own ground type at every metre, blended between the four
// nearest samples and textured with that ground type's colour map.
const ground=program(`#version 300 es
in vec3 aPos;in vec3 aNrm;in vec4 aCol;uniform mat4 uVP;out vec3 vN;out vec3 vP;out vec4 vCol;
void main(){vN=aNrm;vP=aPos;vCol=aCol;gl_Position=uVP*vec4(aPos,1.);}`,`#version 300 es
precision highp float;precision highp sampler2DArray;
in vec3 vN;in vec3 vP;in vec4 vCol;uniform vec3 uEye;uniform vec2 uH;uniform float uFog,uWire,uMode,uLayers,uSide;uniform vec2 uGridAt;
uniform sampler2D uGrid;uniform sampler2DArray uTex;uniform float uRep[32];out vec4 o;
${LIGHT}
${SNOW}
void corner(ivec2 at,float w,inout vec3 acc,inout float ws){int s=int(uSide)-1;at=clamp(at,ivec2(0),ivec2(s));float id=floor(texelFetch(uGrid,at,0).r*255.+.5);
 if(id>=uLayers||w<=0.)return;int i=int(id);acc+=texture(uTex,vec3(vP.xz*uRep[i]/64.,id)).rgb*w;ws+=w;}
void main(){vec3 n=normalize(vN);float h=clamp((vP.y-uH.x)/max(uH.y-uH.x,1.),0.,1.),s=1.-clamp(n.y,0.,1.);
 vec3 c=mix(vec3(.26,.34,.18),vec3(.45,.41,.29),smoothstep(.35,.75,h));c=mix(c,vec3(.40,.39,.37),smoothstep(.18,.45,s));
 if(uMode>.5&&vCol.a>.01)c=vCol.rgb/vCol.a;
 if(uMode>1.5){vec2 g=vP.xz-uGridAt;ivec2 b=ivec2(floor(g));vec2 f=fract(g);vec3 acc=vec3(0.);float ws=0.;
  corner(b,(1.-f.x)*(1.-f.y),acc,ws);corner(b+ivec2(1,0),f.x*(1.-f.y),acc,ws);corner(b+ivec2(0,1),(1.-f.x)*f.y,acc,ws);corner(b+ivec2(1,1),f.x*f.y,acc,ws);
  if(ws>.001)c=acc/ws;}
 c=snowy(c,n,vP,.35);
 vec3 col=shade(c,n,vP);if(uWire>.5)col=mix(col,vec3(1.,.72,.25),.55);o=vec4(pow(col,vec3(1./1.1)),1.);}`);
// Placed objects: one draw per model part, one matrix per placement.
const props=program(`#version 300 es
in vec3 aPos;in vec3 aNrm;in vec2 aUV;in vec4 aM0;in vec4 aM1;in vec4 aM2;in vec4 aM3;uniform mat4 uVP;out vec3 vN;out vec3 vP;out vec2 vUV;
void main(){mat4 m=mat4(aM0,aM1,aM2,aM3);vec4 w=m*vec4(aPos,1.);vP=w.xyz;vN=mat3(m)*aNrm;vUV=aUV;gl_Position=uVP*w;}`,`#version 300 es
precision highp float;in vec3 vN;in vec3 vP;in vec2 vUV;uniform vec3 uEye,uCol,uTint;uniform float uFog,uHasTex;uniform sampler2D uTex;out vec4 o;
${LIGHT}
${SNOW}
void main(){vec3 n=normalize(vN);if(!gl_FrontFacing)n=-n;vec3 base=uHasTex>.5?texture(uTex,vUV).rgb:uCol;o=vec4(pow(shade(snowy(base*uTint,n,vP,.12),n,vP),vec3(1./1.1)),1.);}`);
const loc=(p,names)=>Object.fromEntries(names.map(n=>[n,n.startsWith('a')?gl.getAttribLocation(p,n):gl.getUniformLocation(p,n)]));
const G=loc(ground,['aPos','aNrm','aCol','uVP','uEye','uH','uFog','uWire','uMode','uLayers','uSide','uGridAt','uGrid','uTex','uRep','uSnow','uMeltN','uMelt']);
const PR=loc(props,['aPos','aNrm','aUV','aM0','aM1','aM2','aM3','uVP','uEye','uCol','uTint','uFog','uHasTex','uTex','uSnow','uMeltN','uMelt']);
let snowAmount=0,melts=new Float32Array(96),meltCount=0;
function setSnow(v,list){snowAmount=v;meltCount=Math.min(24,(list||[]).length);melts.fill(0);(list||[]).slice(0,24).forEach((q,i)=>melts.set(q,i*4));}
function snowUniforms(L){gl.uniform1f(L.uSnow,snowAmount);gl.uniform1i(L.uMeltN,meltCount);gl.uniform4fv(L.uMelt,melts);}
const MLOC=[PR.aM0,PR.aM1,PR.aM2,PR.aM3];
function buffer(data,target=gl.ARRAY_BUFFER){const b=gl.createBuffer();gl.bindBuffer(target,b);gl.bufferData(target,data,gl.STATIC_DRAW);return b;}

// ------------------------------------------------------------ ground textures
// One array layer per ground type, filled with its average colour until the
// real colour map arrives, so the ground never flashes black.
let groundTex=null,layerCount=0,reps=new Float32Array(32).fill(16);
function setPalette(pal){
 palette=new Float32Array(256*4);ecoInfo=pal;layerCount=0;
 for(const [id,e] of Object.entries(pal)){const i=+id;if(i<0||i>255)continue;palette.set([...e.colour.map(v=>Math.min(1,v*1.1)),1],i*4);layerCount=Math.max(layerCount,i+1);if(i<32)reps[i]=e.repeat||16;}
 layerCount=Math.min(layerCount,32);if(groundTex)gl.deleteTexture(groundTex);
 groundTex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D_ARRAY,groundTex);gl.texStorage3D(gl.TEXTURE_2D_ARRAY,9,gl.RGBA8,256,256,Math.max(1,layerCount));
 const fill=new Uint8Array(256*256*4);
 for(let i=0;i<layerCount;i++){const c=palette.subarray(i*4,i*4+3);for(let k=0;k<fill.length;k+=4){fill[k]=c[0]*255;fill[k+1]=c[1]*255;fill[k+2]=c[2]*255;fill[k+3]=255;}gl.texSubImage3D(gl.TEXTURE_2D_ARRAY,0,0,0,i,256,256,1,gl.RGBA,gl.UNSIGNED_BYTE,fill);}
 gl.generateMipmap(gl.TEXTURE_2D_ARRAY);gl.texParameteri(gl.TEXTURE_2D_ARRAY,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR);gl.texParameteri(gl.TEXTURE_2D_ARRAY,gl.TEXTURE_WRAP_S,gl.REPEAT);gl.texParameteri(gl.TEXTURE_2D_ARRAY,gl.TEXTURE_WRAP_T,gl.REPEAT);
 const ext=gl.getExtension('EXT_texture_filter_anisotropic');if(ext)gl.texParameterf(gl.TEXTURE_2D_ARRAY,ext.TEXTURE_MAX_ANISOTROPY_EXT,8);
 const mine=groundTex;
 for(const [id,e] of Object.entries(pal)){const i=+id;if(i>=layerCount||!e.texture)continue;const im=new Image();im.onload=()=>{if(groundTex!==mine)return;gl.bindTexture(gl.TEXTURE_2D_ARRAY,groundTex);
   const c=document.createElement('canvas');c.width=c.height=256;c.getContext('2d').drawImage(im,0,0,256,256);
   gl.texSubImage3D(gl.TEXTURE_2D_ARRAY,0,0,0,i,256,256,1,gl.RGBA,gl.UNSIGNED_BYTE,c);gl.generateMipmap(gl.TEXTURE_2D_ARRAY);};
  im.src=`/api/terrain/texture?name=${encodeURIComponent(e.texture)}&size=256`;}
}

// ------------------------------------------------------------ chunks
function uploadChunk(c){
 const pos=b64(c.positions,Float32Array),idx=b64(c.indices,Uint32Array),nrm=new Float32Array(pos.length);
 for(let i=0;i<idx.length;i+=3){const a=idx[i]*3,b=idx[i+1]*3,d=idx[i+2]*3,ux=pos[b]-pos[a],uy=pos[b+1]-pos[a+1],uz=pos[b+2]-pos[a+2],vx=pos[d]-pos[a],vy=pos[d+1]-pos[a+1],vz=pos[d+2]-pos[a+2];let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;if(ny<0){nx=-nx;ny=-ny;nz=-nz;}for(const k of [a,b,d]){nrm[k]+=nx;nrm[k+1]+=ny;nrm[k+2]+=nz;}}
 for(let i=0;i<nrm.length;i+=3){const l=Math.hypot(nrm[i],nrm[i+1],nrm[i+2])||1;nrm[i]/=l;nrm[i+1]/=l;nrm[i+2]/=l;}
 const lines=new Uint32Array(idx.length*2);for(let i=0,j=0;i<idx.length;i+=3){lines[j++]=idx[i];lines[j++]=idx[i+1];lines[j++]=idx[i+1];lines[j++]=idx[i+2];lines[j++]=idx[i+2];lines[j++]=idx[i];}
 let x0=Infinity,z0=Infinity,x1=-Infinity,z1=-Infinity;for(let i=0;i<pos.length;i+=3){x0=Math.min(x0,pos[i]);x1=Math.max(x1,pos[i]);z0=Math.min(z0,pos[i+2]);z1=Math.max(z1,pos[i+2]);}
 const col=new Float32Array(pos.length/3*4);if(c.ecos&&palette){const ec=b64(c.ecos,Uint8Array);for(let i=0;i<ec.length;i++)col.set(palette.subarray(ec[i]*4,ec[i]*4+4),i*4);}
 const vao=gl.createVertexArray();gl.bindVertexArray(vao);
 const bufs=[buffer(pos),buffer(nrm),buffer(col)];
 gl.bindBuffer(gl.ARRAY_BUFFER,bufs[0]);gl.enableVertexAttribArray(G.aPos);gl.vertexAttribPointer(G.aPos,3,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ARRAY_BUFFER,bufs[1]);gl.enableVertexAttribArray(G.aNrm);gl.vertexAttribPointer(G.aNrm,3,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ARRAY_BUFFER,bufs[2]);gl.enableVertexAttribArray(G.aCol);gl.vertexAttribPointer(G.aCol,4,gl.FLOAT,false,0,0);
 const tri=buffer(idx,gl.ELEMENT_ARRAY_BUFFER),wire=buffer(lines,gl.ELEMENT_ARRAY_BUFFER);gl.bindVertexArray(null);
 let gridTex=null,grid=c.grid,gridData=null;
 if(grid){gridData=b64(grid.data,Uint8Array);gridTex=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,gridTex);gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);gl.texImage2D(gl.TEXTURE_2D,0,gl.R8,grid.side,grid.side,0,gl.RED,gl.UNSIGNED_BYTE,gridData);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.NEAREST);gl.pixelStorei(gl.UNPACK_ALIGNMENT,4);}
 return {name:c.name,vao,bufs,tri,wire,gridTex,grid,gridData,count:idx.length,wcount:lines.length,min:c.min,max:c.max,box:[x0,z0,x1,z1],info:c,pos,idx,props:[],objects:[],alive:true};
}
function unloadChunk(name){const c=chunks.get(name);if(!c)return;c.alive=false;gl.deleteVertexArray(c.vao);for(const b of [...c.bufs,c.tri,c.wire])gl.deleteBuffer(b);if(c.gridTex)gl.deleteTexture(c.gridTex);for(const s of c.props)freeSet(s);for(const s of Object.values(c.markerSets||{}))freeSet(s);for(const s of c.treeSets||[])freeSet(s);chunks.delete(name);}
function chunkAtXZ(x,z){for(const c of chunks.values())if(x>=c.box[0]&&x<=c.box[2]&&z>=c.box[1]&&z<=c.box[3])return c;return null;}
function raster(c){ // the chunk's ground height at every metre, rasterised once
 if(c.raster)return c.raster;const gx=c.grid?c.grid.x:Math.floor(c.box[0]),gz=c.grid?c.grid.z:Math.floor(c.box[1]),side=CHUNK+1,h=new Float32Array(side*side).fill(NaN),p=c.pos,ix=c.idx;
 for(let i=0;i<ix.length;i+=3){const a=ix[i]*3,b=ix[i+1]*3,q=ix[i+2]*3,x0=p[a]-gx,z0=p[a+2]-gz,x1=p[b]-gx,z1=p[b+2]-gz,x2=p[q]-gx,z2=p[q+2]-gz,den=(z1-z2)*(x0-x2)+(x2-x1)*(z0-z2);if(Math.abs(den)<1e-9)continue;
  for(let z=Math.max(0,Math.ceil(Math.min(z0,z1,z2)));z<=Math.min(CHUNK,Math.floor(Math.max(z0,z1,z2)));z++)for(let x=Math.max(0,Math.ceil(Math.min(x0,x1,x2)));x<=Math.min(CHUNK,Math.floor(Math.max(x0,x1,x2)));x++){
   const w0=((z1-z2)*(x-x2)+(x2-x1)*(z-z2))/den,w1=((z2-z0)*(x-x2)+(x0-x2)*(z-z2))/den,w2=1-w0-w1;if(w0>=-1e-6&&w1>=-1e-6&&w2>=-1e-6)h[z*side+x]=w0*p[a+1]+w1*p[b+1]+w2*p[q+1];}}
 return c.raster={gx,gz,side,h};}
function zoneSample(x,z){ // height and ground type of the zone at a point, from loaded chunks
 for(const c of chunks.values()){if(c.isArea||x<c.box[0]||x>c.box[2]||z<c.box[1]||z>c.box[3])continue;const r=raster(c),fx=x-r.gx,fz=z-r.gz,i=Math.max(0,Math.min(r.side-2,Math.floor(fx))),j=Math.max(0,Math.min(r.side-2,Math.floor(fz))),u=Math.min(1,Math.max(0,fx-i)),v=Math.min(1,Math.max(0,fz-j)),H=r.h,s=r.side;
  const hs=[H[j*s+i],H[j*s+i+1],H[(j+1)*s+i],H[(j+1)*s+i+1]];if(hs.some(Number.isNaN))continue;
  let eco=null;if(c.gridData){const gs=c.grid.side,gi=Math.max(0,Math.min(gs-1,Math.round(x-c.grid.x))),gj=Math.max(0,Math.min(gs-1,Math.round(z-c.grid.z)));eco=c.gridData[gj*gs+gi];}
  return {h:(hs[0]*(1-u)+hs[1]*u)*(1-v)+(hs[2]*(1-u)+hs[3]*u)*v,eco};}
 return null;}
function heightAt(x,z){ // straight down onto the loaded ground
 const c=chunkAtXZ(x,z);if(!c)return null;if(c.sample)return c.sample(x,z);const p=c.pos,ix=c.idx;
 for(let i=0;i<ix.length;i+=3){const a=ix[i]*3,b=ix[i+1]*3,q=ix[i+2]*3,x0=p[a],z0=p[a+2],x1=p[b],z1=p[b+2],x2=p[q],z2=p[q+2];
  if(x<Math.min(x0,x1,x2)||x>Math.max(x0,x1,x2)||z<Math.min(z0,z1,z2)||z>Math.max(z0,z1,z2))continue;
  const den=(z1-z2)*(x0-x2)+(x2-x1)*(z0-z2);if(Math.abs(den)<1e-9)continue;const w0=((z1-z2)*(x-x2)+(x2-x1)*(z-z2))/den,w1=((z2-z0)*(x-x2)+(x0-x2)*(z-z2))/den,w2=1-w0-w1;
  if(w0>=-1e-6&&w1>=-1e-6&&w2>=-1e-6)return w0*p[a+1]+w1*p[b+1]+w2*p[q+1];}
 return null;}
function normalAt(x,z){const h=heightAt(x,z),hx=heightAt(x+1,z),hz=heightAt(x,z+1);if(h==null||hx==null||hz==null)return [0,1,0];return norm([h-hx,1,h-hz]);}

// ------------------------------------------------------------ models and textures
const meshes=new Map();      // actor|lod -> Promise<geometry>
const textures=new Map();    // name|size -> {tex,ready}
let grey=null;
function texture(name,size){
 const key=name+'|'+size;if(textures.has(key))return textures.get(key);
 const t=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,t);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array([150,150,145,255]));
 const rec={tex:t,ready:false};textures.set(key,rec);
 const im=new Image();im.onload=()=>{gl.bindTexture(gl.TEXTURE_2D,t);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,im);gl.generateMipmap(gl.TEXTURE_2D);
  gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.REPEAT);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.REPEAT);rec.ready=true;};
 im.onerror=()=>{rec.failed=true;};im.src=`/api/terrain/texture?name=${encodeURIComponent(name)}&size=${size}`;return rec;}
function geometry(actor){
 const far=!$('detailProps').checked,key=actor+(far?'|far':'|near');
 if(!meshes.has(key))meshes.set(key,limit(()=>api(`/api/terrain/mesh?actor=${encodeURIComponent(actor)}${far?'&far=1':''}`)).then(m=>{
  const pos=b64(m.positions,Float32Array),nrm=b64(m.normals,Float32Array),uv=m.uvs?b64(m.uvs,Float32Array):new Float32Array(pos.length/3*2),idx=b64(m.indices,Uint32Array);
  let r=0;for(let i=0;i<pos.length;i+=3)r=Math.max(r,Math.hypot(pos[i],pos[i+1],pos[i+2]));
  return {actor,pos,idx,colour:m.colour,tris:m.triangles,radius:r,parts:m.parts||[{first:0,count:idx.length,texture:null}],
   bufs:[buffer(pos),buffer(nrm.length===pos.length?nrm:new Float32Array(pos.length)),buffer(uv)],ibuf:buffer(idx,gl.ELEMENT_ARRAY_BUFFER)};}));
 return meshes.get(key);}
// at most six model requests in flight, so chunks keep streaming meanwhile
let active=0;const waiting=[];
function limit(fn){return new Promise((res,rej)=>{const run=()=>{active++;fn().then(res,rej).finally(()=>{active--;waiting.shift()?.();});};active<6?run():waiting.push(run);});}
function propMatrices(list){ // [x,y,z,yaw,pitch,roll,sx,sy,sz] -> column-major T·Ry·Rx·Rz·S
 const out=new Float32Array(list.length*16);list.forEach((r,i)=>{const [x,y,z,yw,pt,rl,sx,sy,sz]=r,cy=Math.cos(yw),sy_=Math.sin(yw),cp=Math.cos(pt),sp=Math.sin(pt),cr=Math.cos(rl),sr=Math.sin(rl);
  const r00=cy*cr+sy_*sp*sr,r01=-cy*sr+sy_*sp*cr,r02=sy_*cp,r10=cp*sr,r11=cp*cr,r12=-sp,r20=-sy_*cr+cy*sp*sr,r21=sy_*sr+cy*sp*cr,r22=cy*cp;
  out.set([r00*sx,r10*sx,r20*sx,0,r01*sy,r11*sy,r21*sy,0,r02*sz,r12*sz,r22*sz,0,x,y,z,1],i*16);});return out;}
function makeSet(g,instances,opts={}){
 if(!g.idx.length||!instances.length)return null;
 const vao=gl.createVertexArray();gl.bindVertexArray(vao);
 gl.bindBuffer(gl.ARRAY_BUFFER,g.bufs[0]);gl.enableVertexAttribArray(PR.aPos);gl.vertexAttribPointer(PR.aPos,3,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ARRAY_BUFFER,g.bufs[1]);gl.enableVertexAttribArray(PR.aNrm);gl.vertexAttribPointer(PR.aNrm,3,gl.FLOAT,false,0,0);
 gl.bindBuffer(gl.ARRAY_BUFFER,g.bufs[2]);gl.enableVertexAttribArray(PR.aUV);gl.vertexAttribPointer(PR.aUV,2,gl.FLOAT,false,0,0);
 const inst=buffer(propMatrices(instances));gl.bindBuffer(gl.ARRAY_BUFFER,inst);
 MLOC.forEach((l,i)=>{gl.enableVertexAttribArray(l);gl.vertexAttribPointer(l,4,gl.FLOAT,false,64,i*16);gl.vertexAttribDivisor(l,1);});
 gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,g.ibuf);gl.bindVertexArray(null);
 let cx=0,cy=0,cz=0;for(const r of instances){cx+=r[0];cy+=r[1];cz+=r[2];}const n=instances.length;cx/=n;cy/=n;cz/=n;
 let spread=0;for(const r of instances)spread=Math.max(spread,Math.hypot(r[0]-cx,r[2]-cz)+g.radius*Math.max(r[6],r[7],r[8]));
 return {vao,inst,g,n,centre:[cx,cy,cz],spread,small:g.radius<2.5,tint:opts.tint||[1,1,1]};}
function freeSet(s){gl.deleteVertexArray(s.vao);gl.deleteBuffer(s.inst);}

async function loadChunkProps(c){
 const [x0,z0]=c.grid?[c.grid.x,c.grid.z]:[c.box[0],c.box[1]];
 let groups;try{groups=(await api(`/api/terrain/objects?zone=${zone}&x0=${x0}&z0=${z0}&x1=${x0+CHUNK}&z1=${z0+CHUNK}`)).groups;}catch(err){return;}
 if(!c.alive)return;c.objects=groups;
 groups.sort((a,b)=>b.instances.length-a.instances.length);
 await Promise.all(groups.map(async grp=>{try{const g=await geometry(grp.actor);if(!c.alive)return;const s=makeSet(g,grp.instances);if(s){s.actor=grp.actor;c.props.push(s);}}catch(err){}}));
 info();}
function reloadProps(){for(const c of chunks.values()){for(const s of c.props)freeSet(s);c.props=[];loadChunkProps(c);}}

// ------------------------------------------------------------ spawn markers and trees
// Loot spawners, vehicle spawn points and doors come from the zone file; player
// spawns, server vehicle spawns, named places and trees from the emulator data.
let layerInfo=[],places=null;const layerOn={};
const LAYER_DEFAULT={player:1,vehicleSpawn:1,vehicle:1,weapons:1,crate:1,trees:1,places:1};
const hex=c=>[1,3,5].map(i=>parseInt(c.slice(i,i+2),16)/255);
const colourOf=k=>hex((layerInfo.find(l=>l.key===k)||{}).colour||'#ffffff');
function meshFrom(parts,colour=[1,1,1]){
 const pos=[],nrm=[],idx=[];for(const p of parts)for(const f of p.f){const a=p.v[f[0]],b=p.v[f[1]],c=p.v[f[2]],n=norm(cross([b[0]-a[0],b[1]-a[1],b[2]-a[2]],[c[0]-a[0],c[1]-a[1],c[2]-a[2]]));const base=pos.length/3;for(const q of [a,b,c]){pos.push(...q);nrm.push(...n);}idx.push(base,base+1,base+2);}
 const P=new Float32Array(pos),I=new Uint32Array(idx);let r=0;for(let i=0;i<P.length;i+=3)r=Math.max(r,Math.hypot(P[i],P[i+1],P[i+2]));
 return {pos:P,idx:I,colour,tris:I.length/3,radius:r,parts:[{first:0,count:I.length,texture:null}],bufs:[buffer(P),buffer(new Float32Array(nrm)),buffer(new Float32Array(P.length/3*2))],ibuf:buffer(I,gl.ELEMENT_ARRAY_BUFFER)};}
function prism(r,y0,y1,n){const v=[],f=[];for(let i=0;i<n;i++){const a=i/n*Math.PI*2;v.push([Math.cos(a)*r,y0,Math.sin(a)*r],[Math.cos(a)*r,y1,Math.sin(a)*r]);}for(let i=0;i<n;i++){const j=(i+1)%n;f.push([i*2,j*2,i*2+1],[i*2+1,j*2,j*2+1]);}return {v,f};}
function cone(r,y0,y1,n){const v=[[0,y1,0],[0,y0,0]],f=[];for(let i=0;i<n;i++){const a=i/n*Math.PI*2;v.push([Math.cos(a)*r,y0,Math.sin(a)*r]);}for(let i=0;i<n;i++){const a=2+i,b=2+(i+1)%n;f.push([0,b,a],[1,a,b]);}return {v,f};}
function octa(r,y,s=1){const v=[[0,y+r*s,0],[0,y-r*s,0],[r,y,0],[-r,y,0],[0,y,r],[0,y,-r]];return {v,f:[[0,2,4],[0,4,3],[0,3,5],[0,5,2],[1,4,2],[1,3,4],[1,5,3],[1,2,5]]};}
// a map pin: pole and a diamond head, tinted by its layer colour
const PIN=meshFrom([prism(.07,0,2.4,6),octa(.55,3,1.4)]);
const TREES={pine:meshFrom([prism(.25,0,3,6),cone(2.6,2.2,7,8),cone(2,5,10,8),cone(1.3,8,13,8)],[.16,.3,.15]),
 broadleaf:meshFrom([prism(.3,0,4,6),octa(3.4,6.4,.8),octa(2.4,8.2,.8)],[.27,.42,.18]),bush:meshFrom([octa(1.5,1,.7)],[.24,.36,.16])};
const TREE_OF={10:['pine',1],7:['pine',1.45],14:['broadleaf',1],12:['bush',1]};
async function loadChunkMarkers(c){
 const [x0,z0]=c.grid?[c.grid.x,c.grid.z]:[c.box[0],c.box[1]],q=`zone=${zone}&x0=${x0}&z0=${z0}&x1=${x0+CHUNK}&z1=${z0+CHUNK}`;
 let m,tr;try{[m,tr]=await Promise.all([api('/api/terrain/markers?'+q),api('/api/terrain/trees?'+q)]);}catch(err){return;}
 if(!c.alive)return;c.markerRows=m;c.markerSets={};c.treeSets=[];
 for(const [k,rows] of Object.entries(m)){if(!rows.length)continue;const s=makeSet(PIN,rows.map(r=>[r[0],r[1],r[2],r[3],0,0,1.2,1.2,1.2]),{tint:colourOf(k)});if(s){s.key=k;c.markerSets[k]=s;}}
 const byType={};for(const r of tr.trees){const [kind,size]=TREE_OF[r[3]]||['pine',1];const sc=size*(.75+(r[4]%97)/97*.6);(byType[kind]=byType[kind]||[]).push([r[0],r[1],r[2],(r[4]%360)*Math.PI/180,0,0,sc,sc,sc]);}
 for(const [kind,rows] of Object.entries(byType)){const s=makeSet(TREES[kind],rows);if(s)c.treeSets.push(s);}
 info();}
function renderLayers(){
 const rows=[...layerInfo,...(places?.places?.length?[{key:'places',label:'Place names',colour:'#f5a524',count:places.places.length}]:[])];
 $('layerList').innerHTML=rows.map(l=>`<label class="chk layer"><input type="checkbox" data-layer="${l.key}" ${layerOn[l.key]?'checked':''}><i style="background:${l.colour}"></i><span>${l.label}</span><small>${l.count.toLocaleString()}</small></label>`).join('')||'<div class="legend">No spawn data for this zone.</div>';}
$('layerList').addEventListener('change',e=>{const k=e.target.dataset.layer;if(!k)return;layerOn[k]=e.target.checked;try{localStorage.setItem('terrain.layers',JSON.stringify(layerOn));}catch(err){}drawMap();});
async function loadLayers(z){
 try{layerInfo=(await api('/api/terrain/layers?zone='+encodeURIComponent(z))).layers;}catch(err){layerInfo=[];}
 try{places=await api('/api/terrain/places?zone='+encodeURIComponent(z));}catch(err){places=null;}
 let saved={};try{saved=JSON.parse(localStorage.getItem('terrain.layers')||'{}');}catch(err){}
 for(const l of [...layerInfo,{key:'places'}])layerOn[l.key]=l.key in saved?saved[l.key]:!!LAYER_DEFAULT[l.key];
 for(const c of chunks.values())for(const s of Object.values(c.markerSets||{}))s.tint=colourOf(s.key);   // loaded before the colours arrived
 renderLayers();drawMap();}

// ------------------------------------------------------------ camera
const cam={target:[0,60,0],yaw:.8,pitch:.5,dist:450},vel={yaw:0,pitch:0};
const mul=(a,b)=>{const o=new Float32Array(16);for(let c=0;c<4;c++)for(let r=0;r<4;r++){let s=0;for(let k=0;k<4;k++)s+=a[k*4+r]*b[c*4+k];o[c*4+r]=s;}return o;};
function eye(){const c=Math.cos(cam.pitch);return [cam.target[0]+cam.dist*c*Math.sin(cam.yaw),cam.target[1]+cam.dist*Math.sin(cam.pitch),cam.target[2]+cam.dist*c*Math.cos(cam.yaw)];}
function viewProj(){const e=eye(),t=cam.target,f=norm([t[0]-e[0],t[1]-e[1],t[2]-e[2]]),s=norm(cross(f,[0,1,0])),u=cross(s,f);
 const v=new Float32Array([s[0],u[0],-f[0],0,s[1],u[1],-f[1],0,s[2],u[2],-f[2],0,-dot(s,e),-dot(u,e),dot(f,e),1]);
 const n=Math.max(.5,cam.dist*.004),far=cam.dist*12+9000,a=cv.width/Math.max(1,cv.height),fy=1/Math.tan(.45);
 const p=new Float32Array([fy/a,0,0,0,0,fy,0,0,0,0,(far+n)/(n-far),-1,0,0,2*far*n/(n-far),0]);return {m:mul(p,v),e};}
function frame(){if(!chunks.size)return;let x0=Infinity,z0=Infinity,x1=-Infinity,z1=-Infinity,y0=Infinity,y1=-Infinity;for(const c of chunks.values()){x0=Math.min(x0,c.box[0]);z0=Math.min(z0,c.box[1]);x1=Math.max(x1,c.box[2]);z1=Math.max(z1,c.box[3]);y0=Math.min(y0,c.min);y1=Math.max(y1,c.max);}
 cam.target=[(x0+x1)/2,(y0+y1)/2,(z0+z1)/2];cam.dist=Math.max(x1-x0,z1-z0,200)*.9;cam.pitch=.6;vel.yaw=vel.pitch=0;}
function flyTo(x,z){cam.target=[x,heightAt(x,z)??cam.target[1],z];if(cam.dist>900)cam.dist=450;$('empty').hidden=true;if(!$('auto').checked)enqueue(around(x,z,+$('range').value));}

// ------------------------------------------------------------ render loop
const keys=new Set();let last=0,fps=0,frames=0,fpsAt=0,streamAt=0;
function render(ts){requestAnimationFrame(render);const dt=Math.min(.05,(ts-last)/1000||0);last=ts;frames++;if(ts-fpsAt>1000){fps=frames;frames=0;fpsAt=ts;info();}
 if(!drag&&(Math.abs(vel.yaw)>1e-4||Math.abs(vel.pitch)>1e-4)){cam.yaw+=vel.yaw;cam.pitch=Math.max(.03,Math.min(1.55,cam.pitch+vel.pitch));const k=Math.pow(.003,dt);vel.yaw*=k;vel.pitch*=k;}
 move(dt);
 const w=cv.clientWidth,h=cv.clientHeight,dpr=Math.min(2,devicePixelRatio||1);if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr);}
 gl.viewport(0,0,cv.width,cv.height);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);
 if(!chunks.size&&!roomSets.length)return;
 const {m,e}=viewProj(),fog=1/Math.max(1500,+$('propRange').value*2.2+cam.dist*2);let y0=Infinity,y1=-Infinity;for(const c of chunks.values()){y0=Math.min(y0,c.min);y1=Math.max(y1,c.max);}
 gl.useProgram(ground);gl.uniformMatrix4fv(G.uVP,false,m);gl.uniform3fv(G.uEye,e);gl.uniform2f(G.uH,y0,y1);gl.uniform1f(G.uFog,fog);snowUniforms(G);
 const wire=$('wire').checked,textured=$('textures').checked&&groundTex&&layerCount;gl.uniform1f(G.uWire,wire?1:0);gl.uniform1f(G.uLayers,layerCount);gl.uniform1fv(G.uRep,reps);
 gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D_ARRAY,groundTex);gl.uniform1i(G.uTex,1);gl.uniform1i(G.uGrid,0);
 for(const c of chunks.values()){gl.uniform1f(G.uMode,textured&&c.gridTex?2:palette?1:0);if(c.gridTex){gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,c.gridTex);gl.uniform2f(G.uGridAt,c.grid.x,c.grid.z);gl.uniform1f(G.uSide,c.grid.side);}
  gl.bindVertexArray(c.vao);if(wire){gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,c.wire);gl.drawElements(gl.LINES,c.wcount,gl.UNSIGNED_INT,0);}else{gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,c.tri);gl.drawElements(gl.TRIANGLES,c.count,gl.UNSIGNED_INT,0);}}
 gl.bindVertexArray(null);
 gl.useProgram(props);gl.uniformMatrix4fv(PR.uVP,false,m);gl.uniform3fv(PR.uEye,e);gl.uniform1f(PR.uFog,fog);gl.uniform1i(PR.uTex,0);gl.activeTexture(gl.TEXTURE0);snowUniforms(PR);
 const range=+$('propRange').value,texOn=$('textures').checked,size=+$('texQuality').value;drawn=0;
 const planes=frustum(m);
 if($('objects').checked)for(const c of chunks.values())for(const s of c.props){const d=Math.hypot(s.centre[0]-e[0],s.centre[1]-e[1],s.centre[2]-e[2])-s.spread;if(d>range||(s.small&&d>range*.35)||!inView(planes,s.centre,s.spread+10))continue;drawSet(s,texOn,size);}
 if(layerOn.trees)for(const c of chunks.values())for(const s of c.treeSets||[]){const d=Math.hypot(s.centre[0]-e[0],s.centre[1]-e[1],s.centre[2]-e[2])-s.spread;if(d>range*1.6||!inView(planes,s.centre,s.spread+15))continue;drawSet(s,false,size);}
 for(const c of chunks.values())for(const s of Object.values(c.markerSets||{})){if(!layerOn[s.key])continue;const d=Math.hypot(s.centre[0]-e[0],s.centre[1]-e[1],s.centre[2]-e[2])-s.spread;if(d>Math.max(700,range)||!inView(planes,s.centre,s.spread+10))continue;drawSet(s,false,size);}
 for(const s of roomSets)drawSet(s,texOn,size);
 if(hooks.draw)hooks.draw(m,e,{texOn,size,fog,ts});
 gl.bindVertexArray(null);}
let drawn=0;
// the six clip planes of a view-projection matrix, for skipping what is off screen
function frustum(m){const r=i=>[m[i],m[4+i],m[8+i],m[12+i]],r0=r(0),r1=r(1),r2=r(2),r3=r(3);
 return [0,1,2].flatMap(k=>{const a=[r0,r1,r2][k];return [r3.map((v,i)=>v+a[i]),r3.map((v,i)=>v-a[i])];}).map(p=>{const l=Math.hypot(p[0],p[1],p[2])||1;return p.map(v=>v/l);});}
function inView(planes,c,radius){for(const p of planes)if(p[0]*c[0]+p[1]*c[1]+p[2]*c[2]+p[3]<-radius)return false;return true;}
function drawSet(s,texOn,size){gl.bindVertexArray(s.vao);gl.uniform3fv(PR.uTint,s.tint);
 for(const p of s.g.parts){const t=texOn&&p.texture?texture(p.texture,size):null;gl.uniform1f(PR.uHasTex,t&&t.ready?1:0);gl.uniform3fv(PR.uCol,s.g.colour);if(t)gl.bindTexture(gl.TEXTURE_2D,t.tex);
  gl.drawElementsInstanced(gl.TRIANGLES,p.count,gl.UNSIGNED_INT,p.first*4,s.n);}drawn+=s.n;}
requestAnimationFrame(render);
function move(dt){
 let fx=0,fz=0;if(keys.has('w')||keys.has('arrowup'))fz-=1;if(keys.has('s')||keys.has('arrowdown'))fz+=1;if(keys.has('a')||keys.has('arrowleft'))fx-=1;if(keys.has('d')||keys.has('arrowright'))fx+=1;
 if(!fx&&!fz)return;const speed=Math.max(25,cam.dist*1.1)*(keys.has('shift')?3.5:1)*dt,sy=Math.sin(cam.yaw),cy=Math.cos(cam.yaw);
 cam.target[0]+=(fx*cy+fz*sy)*speed;cam.target[2]+=(-fx*sy+fz*cy)*speed;const h=heightAt(cam.target[0],cam.target[2]);if(h!=null)cam.target[1]+=(h-cam.target[1])*Math.min(1,dt*6);$('empty').hidden=true;}

// ------------------------------------------------------------ streaming
function around(x,z,ring){if(!overview)return [];const cx=Math.floor(x/CHUNK),cz=Math.floor(z/CHUNK);
 return overview.chunks.filter(c=>Math.abs(Math.floor(c.x/CHUNK)-cx)<=ring&&Math.abs(Math.floor(c.z/CHUNK)-cz)<=ring)
  .sort((a,b)=>Math.hypot(a.x+128-x,a.z+128-z)-Math.hypot(b.x+128-x,b.z+128-z)).map(c=>c.name);}
function stream(){if(!overview||!$('auto').checked||$('empty').hidden===false||(hooks.frozen&&hooks.frozen()))return;const ring=+$('range').value,[x,,z]=cam.target;
 const want=new Set(around(x,z,ring));queue=queue.filter(n=>want.has(n));for(const n of [...state.keys()])if(state.get(n)==='loading'&&!want.has(n)&&!queue.includes(n)&&n!==current)state.delete(n);
 enqueue([...want]);for(const n of [...chunks.keys()]){const c=chunks.get(n);if(c.isArea)continue;if(Math.max(Math.abs(c.box[0]+128-x),Math.abs(c.box[1]+128-z))>(ring+1.6)*CHUNK){unloadChunk(n);}}drawMap();}
setInterval(()=>stream(),400);   // not tied to drawing: keeps loading while the page is in the background
function enqueue(names){let added=false;for(const n of names)if(!chunks.has(n)&&state.get(n)!=='loading'&&state.get(n)!=='error'&&!queue.includes(n)){queue.push(n);state.set(n,'loading');added=true;}if(added){drawMap();pump();}}
let current=null;
async function pump(){
 if(working)return;working=true;const gen=generation;
 try{if(!palette&&paletteLoad)await paletteLoad.catch(()=>{});
  while(queue.length&&gen===generation){const name=current=queue.shift();$('load').hidden=false;$('load').textContent=`Loading ground ${name.replace('.cnk','')}${queue.length?` · ${queue.length} more`:''}`;
   try{const c=await api(`/api/terrain/chunk?name=${encodeURIComponent(name)}&detail=${$('detail').value}`);if(gen!==generation||state.get(name)!=='loading')continue;
    const rec=uploadChunk(c);chunks.set(name,rec);state.delete(name);$('empty').hidden=true;loadChunkProps(rec);loadChunkMarkers(rec);
   }catch(err){state.set(name,'error');status(`${name.replace('.cnk','')}: ${err.message}`,true);}
   drawMap();info();}
 }finally{working=false;current=null;$('load').hidden=true;}}
function unloadAll(){++generation;queue=[];state.clear();for(const n of [...chunks.keys()])if(!chunks.get(n).isArea)unloadChunk(n);info();drawMap();}

// ------------------------------------------------------------ controls
let drag=null;
cv.addEventListener('contextmenu',e=>e.preventDefault());
cv.addEventListener('pointerdown',e=>{cv.focus();try{cv.setPointerCapture(e.pointerId);}catch(err){}vel.yaw=vel.pitch=0;
 if(hooks.down&&hooks.down(e))return;
 // dragging the selected room object moves it along the ground
 if(e.button===0&&!e.shiftKey&&mode==='select'&&selSpawn>=0&&room.spawns[selSpawn]){const hit=groundHit(e),s=room.spawns[selSpawn];
  if(hit&&Math.hypot(hit[0]-s.pos[0],hit[2]-s.pos[2])<3){drag={moveSpawn:true,sx:e.clientX,sy:e.clientY,before:snapshot()};return;}}
 if(e.button===0&&!e.shiftKey&&mode==='select'&&selected>=0&&room.objects[selected]){const hit=groundHit(e),o=room.objects[selected];
  if(hit&&Math.hypot(hit[0]-o.pos[0],hit[2]-o.pos[2])<Math.max(3,(meshRadius(o.actor)||2)*o.scale[0])){drag={moveObject:true,sx:e.clientX,sy:e.clientY,before:snapshot()};return;}}
 const tool=hooks.active&&hooks.active();
 drag={x:e.clientX,y:e.clientY,sx:e.clientX,sy:e.clientY,button:e.button,pan:tool?e.shiftKey:(e.button!==0||e.shiftKey),tool};cv.classList.add('drag');});
cv.addEventListener('pointermove',e=>{if(hooks.move&&hooks.move(e,!!drag))return;if(!drag)return;
 if(drag.moveSpawn){const s=room.spawns[selSpawn];if(!s)return;const at=planeHit(e,s.pos[1]);if(!at)return;const x=snapV(at[0]),z=snapV(at[2]);s.pos=[x,heightAt(x,z)??s.pos[1],z].map(v=>+v.toFixed(3));drag.moved=true;queueRoom();return;}
 if(drag.moveObject){const o=room.objects[selected];if(!o)return;const at=planeHit(e,o.pos[1]);if(!at)return;const x=snapV(at[0]),z=snapV(at[2]);o.pos=[x,heightAt(x,z)??o.pos[1],z].map(v=>+v.toFixed(3));if($('alignGround').checked)alignToGround(o);drag.moved=true;queueRoom();return;}
 const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;
 if(drag.pan){const k=cam.dist/Math.max(300,cv.clientHeight)*1.2,sy=Math.sin(cam.yaw),cy=Math.cos(cam.yaw);cam.target[0]-=(dx*cy-dy*sy)*k;cam.target[2]-=(-dx*sy-dy*cy)*k;}
 else{cam.yaw-=dx*.006;cam.pitch=Math.max(.03,Math.min(1.55,cam.pitch+dy*.006));vel.yaw=-dx*.006*.3;vel.pitch=dy*.006*.3;}});
const up=e=>{if(hooks.up&&hooks.up(e))return;const d=drag;drag=null;cv.classList.remove('drag');if(!d||d.tool)return;
 if(d.moveObject||d.moveSpawn){if(d.moved){pushHistory(d.before);status('Moved.');}else buildClick(e);return;}
 if(e&&d.button===0&&!d.pan&&Math.hypot(e.clientX-d.sx,e.clientY-d.sy)<5)buildClick(e);};
cv.addEventListener('pointerup',up);cv.addEventListener('pointercancel',up);
cv.addEventListener('wheel',e=>{e.preventDefault();cam.dist=Math.max(4,Math.min(40000,cam.dist*Math.exp(Math.max(-1,Math.min(1,e.deltaY/400)))));},{passive:false});
const typing=e=>/INPUT|SELECT|TEXTAREA/.test(e.target.tagName);
addEventListener('keydown',e=>{if(typing(e))return;const k=e.key.toLowerCase();if(hooks.key&&hooks.key(e))return;
 if(['w','a','s','d','arrowup','arrowdown','arrowleft','arrowright','shift'].includes(k)&&!e.ctrlKey){keys.add(k);if(k!=='shift')e.preventDefault();return;}
 if(k==='f'&&!e.ctrlKey){frame();return;}buildKey(e);});
addEventListener('keyup',e=>keys.delete(e.key.toLowerCase()));addEventListener('blur',()=>keys.clear());

// ------------------------------------------------------------ overview map
const map=$('map'),mc=map.getContext('2d');
function mapXY(x,z){const b=overview.bounds,s=Math.max(b[2]-b[0],b[3]-b[1]),pad=10,W=map.width-pad*2;return [pad+(x-b[0])/s*W,pad+(z-b[1])/s*W];}
function mapWorld(px,py){const b=overview.bounds,s=Math.max(b[2]-b[0],b[3]-b[1]),pad=10,W=map.width-pad*2;return [b[0]+(px-pad)/W*s,b[1]+(py-pad)/W*s];}
let mapDots=null;
function drawMap(){
 mc.clearRect(0,0,map.width,map.height);if(!overview)return;
 if(!mapDots){mapDots=document.createElement('canvas');mapDots.width=map.width;mapDots.height=map.height;const d=mapDots.getContext('2d'),p=overview.points;d.fillStyle='rgba(163,171,183,.3)';for(let i=0;i<p.length;i+=2){const [x,y]=mapXY(p[i],p[i+1]);d.fillRect(x,y,1.3,1.3);}}
 mc.drawImage(mapDots,0,0);
 for(const c of overview.chunks){const s=chunks.has(c.name)?'loaded':state.get(c.name);if(!s&&c!==hover)continue;const [x,y]=mapXY(c.x,c.z),[x2,y2]=mapXY(c.x+c.size,c.z+c.size);
  mc.fillStyle=s==='loaded'?'rgba(245,165,36,.32)':s==='loading'?'rgba(98,179,255,.3)':s==='error'?'rgba(242,95,92,.4)':'transparent';mc.fillRect(x,y,x2-x,y2-y);
  if(c===hover){mc.strokeStyle='#f5a524';mc.lineWidth=2;mc.strokeRect(x+.5,y+.5,x2-x-1,y2-y-1);}}
 // where the camera is and which way it looks
 const [cx,cy]=mapXY(cam.target[0],cam.target[2]),a=-cam.yaw;mc.save();mc.translate(cx,cy);mc.rotate(a);mc.fillStyle='#62b3ff';mc.beginPath();mc.moveTo(0,-9);mc.lineTo(6,6);mc.lineTo(-6,6);mc.closePath();mc.fill();mc.restore();
 if(places){
  const dot=(list,key,r)=>{if(!layerOn[key]||!list)return;mc.fillStyle=(layerInfo.find(l=>l.key===key)||{}).colour||'#fff';for(const p of list){const [x,y]=mapXY(p[0],p[1]);mc.fillRect(x-r/2,y-r/2,r,r);}};
  dot(places.weapons,'weapons',1.6);dot(places.vehicle,'vehicle',3);dot(places.vehicleSpawn,'vehicleSpawn',4);dot(places.player,'player',4.5);
  if(layerOn.places)for(const pl of places.places){mc.strokeStyle='rgba(245,165,36,.55)';mc.lineWidth=1;for(const poly of pl.bounds||[]){if(poly.length<3)continue;mc.beginPath();poly.forEach((pt,i)=>{const [x,y]=mapXY(pt[0],pt[1]);i?mc.lineTo(x,y):mc.moveTo(x,y);});mc.closePath();mc.stroke();}
   if(pl.position){const [x,y]=mapXY(pl.position[0],pl.position[2]);mc.font='600 13px Segoe UI, sans-serif';mc.textAlign='center';mc.lineWidth=3;mc.strokeStyle='rgba(10,11,13,.85)';mc.strokeText(pl.name,x,y);mc.fillStyle='#ffd08a';mc.fillText(pl.name,x,y);}}}
 for(const s of room.spawns||[]){const [sx,sy]=mapXY(s.pos[0],s.pos[2]);mc.fillStyle='#39ff9f';mc.beginPath();mc.arc(sx,sy,3.5,0,7);mc.fill();}
 if(hooks.map)hooks.map(mc,mapXY);
}
function mapEvent(ev){const r=map.getBoundingClientRect();return [(ev.clientX-r.left)/r.width*map.width,(ev.clientY-r.top)/r.height*map.height];}
function chunkAt(ev){if(!overview)return null;const [px,py]=mapEvent(ev);return overview.chunks.find(c=>{const [x,y]=mapXY(c.x,c.z),[x2,y2]=mapXY(c.x+c.size,c.z+c.size);return px>=x&&px<x2&&py>=y&&py<y2;})||null;}
map.addEventListener('mousemove',e=>{const c=chunkAt(e);if(c!==hover){hover=c;drawMap();}map.title=c?c.name.replace('.cnk',''):'';});
map.addEventListener('mouseleave',()=>{hover=null;drawMap();});
map.addEventListener('click',e=>{if(!overview)return;const [x,z]=mapWorld(...mapEvent(e));flyTo(x,z);if(e.shiftKey){const c=chunkAt(e);if(c)enqueue([c.name]);}status('Flying there. The ground loads around the view; W A S D to move.');drawMap();});

function info(){
 if(!chunks.size){$('info').innerHTML='';return;}
 let v=0,objs=0,sets=0,y0=Infinity,y1=-Infinity;for(const c of chunks.values()){v+=c.info.vertices;y0=Math.min(y0,c.min);y1=Math.max(y1,c.max);sets+=c.props.length;for(const s of c.props)objs+=s.n;}
 const t=cam.target;let spawnView=0,treeView=0;for(const c of chunks.values()){for(const [k,rows] of Object.entries(c.markerRows||{}))if(layerOn[k])spawnView+=rows.length;for(const s of c.treeSets||[])treeView+=s.n;}
 $('info').innerHTML=`<b>${zone}</b> · ${chunks.size} chunk${chunks.size===1?'':'s'}${queue.length?` · ${queue.length} queued`:''}<br>${objs.toLocaleString()} objects · ${sets} model sets<br>${spawnView.toLocaleString()} spawn markers shown · ${treeView.toLocaleString()} trees<br>height ${y0.toFixed(0)} to ${y1.toFixed(0)} m<br><span style="color:#6c7482">x ${t[0].toFixed(0)} · z ${t[2].toFixed(0)} · ${fps} fps</span>`;}
$('clear').onclick=()=>{unloadAll();$('empty').hidden=false;};
$('frameBtn').onclick=frame;
$('detail').onchange=()=>{const names=[...chunks.keys()].filter(n=>!chunks.get(n).isArea);unloadAll();enqueue(names);};
$('detailProps').onchange=reloadProps;
$('zone').onchange=()=>{actors=[];openZone($('zone').value).then(()=>{if($('builder').open)loadActors();});};
let game='steam';
async function loadZones(prefer){const {zones}=await api('/api/terrain/zones');$('zone').innerHTML=zones.map(z=>`<option>${z}</option>`).join('');
 if(!zones.length){status('No zone files found in this client.',true);return;}await openZone(zones.includes(prefer)?prefer:zones.includes('Z1')?'Z1':zones[0]);$('zone').value=zone;}
async function setGame(g,prefer){const r=await api('/api/terrain/game?set='+encodeURIComponent(g));game=r.current;$('game').value=game;
 unloadAll();places=null;layerInfo=[];renderLayers();await loadZones(prefer||(game==='kotk'?'Z2':'Z1'));if($('builder').open)loadActors();}
$('game').onchange=()=>setGame($('game').value).catch(err=>status(err.message,true));

async function openZone(z){zone=z;unloadAll();overview=null;mapDots=null;places=null;layerInfo=[];drawMap();loadLayers(z);palette=null;paletteLoad=api('/api/terrain/palette?zone='+encodeURIComponent(z)).then(setPalette);paletteLoad.catch(()=>{});
 status(`Reading ${z}.zone… the first time takes a few seconds.`);$('empty').hidden=false;
 try{overview=await api('/api/terrain/overview?zone='+encodeURIComponent(z));const b=overview.bounds;cam.target=[(b[0]+b[2])/2,60,(b[1]+b[3])/2];drawMap();
  status(`${z}: ${overview.chunks.length} terrain chunks, ${overview.objects.toLocaleString()} placed objects. Click the map to fly there.`);}
 catch(err){status(err.message,true);}}

// ------------------------------------------------------------ room builder
// A room is placed objects plus typed spawn points (players, loot, vehicles),
// saved as JSON for the server to spawn from.
let room={name:'',zone:'Z1',spawn:null,objects:[],spawns:[]},selected=-1,selSpawn=-1,mode=null,roomSets=[],token='',actors=[],history=[],future=[],scenePick=null;
const SPAWN_KINDS={player:['Player','#39ff9f'],weapons:['Weapon','#f25f5c'],gear:['Gear','#f0c04a'],backpack:['Backpack','#b07cff'],medical:['Medical','#3fcf8e'],crate:['Military crate','#ff8a3d'],fuel:['Fuel can','#62b3ff'],vehicle:['Vehicle','#27d3ff']};
// game layers that become room spawn kinds when copied in
const LAYER_KIND={player:'player',weapons:'weapons',gear:'gear',backpack:'backpack',medical:'medical',crate:'crate',fuel:'fuel',vehicle:'vehicle',vehicleSpawn:'vehicle'};
const radii=new Map();function meshRadius(actor){return radii.get(actor);}
const snapshot=()=>JSON.stringify(room);
function pushHistory(before){history.push(before);if(history.length>100)history.shift();future=[];}
function change(fn){const before=snapshot();fn();pushHistory(before);queueRoom();}
function normaliseRoom(r){r.spawns=r.spawns||[];if(r.spawn&&!r.spawns.length)r.spawns=[{kind:'player',detail:'',pos:r.spawn.slice(0,3),yaw:r.spawn[3]||0}];return r;}
function restore(json){room=normaliseRoom(JSON.parse(json));if(selected>=room.objects.length)selected=-1;if(selSpawn>=room.spawns.length)selSpawn=-1;queueRoom();}
// coalesces bursts (a drag fires many moves) without waiting on a paint, which
// a background page never gets
let roomQueued=false;function queueRoom(){if(roomQueued)return;roomQueued=true;setTimeout(()=>{roomQueued=false;rebuildRoom();},16);}
let roomBuild=0;
async function rebuildRoom(){
 const n=++roomBuild,groups=new Map();room.objects.forEach((o,i)=>{if(i===selected)return;if(!groups.has(o.actor))groups.set(o.actor,[]);groups.get(o.actor).push(o);});
 const sets=[],row=o=>[...o.pos,...o.rot,...o.scale];
 for(const [actor,list] of groups){try{const g=await geometry(actor);radii.set(actor,g.radius);const s=makeSet(g,list.map(row));if(s)sets.push(s);}catch(err){status(`${actor}: ${err.message}`,true);}}
 if(selected>=0&&room.objects[selected]){const o=room.objects[selected];try{const g=await geometry(o.actor);radii.set(o.actor,g.radius);const s=makeSet(g,[row(o)],{tint:[1.5,1.15,.55]});if(s)sets.push(s);}catch(err){}}
 // your spawns: bigger pins than the game's, selected one in amber
 const byKind={};room.spawns.forEach((s,i)=>{const k=i===selSpawn?'__sel':s.kind;(byKind[k]=byKind[k]||[]).push([s.pos[0],s.pos[1],s.pos[2],s.yaw||0,0,0,1.9,1.9,1.9]);});
 for(const [k,rows] of Object.entries(byKind)){const s=makeSet(PIN,rows,{tint:k==='__sel'?[1,.66,.14]:hex((SPAWN_KINDS[k]||['','#ffffff'])[1])});if(s)sets.push(s);}
 if(n!==roomBuild){sets.forEach(freeSet);return;}
 for(const s of roomSets)freeSet(s);roomSets=sets;roomInfo();drawMap();}
function roomInfo(){const o=room.objects[selected],sp=room.spawns[selSpawn],counts={};for(const s of room.spawns)counts[s.kind]=(counts[s.kind]||0)+1;
 $('roomInfo').innerHTML=`${room.objects.length} object${room.objects.length===1?'':'s'} · ${room.spawns.length} spawn${room.spawns.length===1?'':'s'}`+(room.spawns.length?`<br>${Object.entries(counts).map(([k,n])=>`${n} ${SPAWN_KINDS[k]?.[0].toLowerCase()||k}`).join(' · ')}`:'')
  +(o?`<br><b>Selected:</b> ${o.actor.replace('.adr','')}<br>yaw ${Math.round(o.rot[0]*180/Math.PI)}° · scale ${o.scale[0].toFixed(2)} · at ${o.pos.map(v=>v.toFixed(1)).join(', ')}`:'')
  +(sp?`<br><b>Selected spawn:</b> ${SPAWN_KINDS[sp.kind]?.[0]||sp.kind}${sp.detail?' · '+sp.detail:''}<br>yaw ${Math.round((sp.yaw||0)*180/Math.PI)}° · at ${sp.pos.map(v=>v.toFixed(1)).join(', ')}`:'');
 $('undo').disabled=!history.length;$('redo').disabled=!future.length;$('dup').disabled=selected<0&&selSpawn<0;}
const HINTS={place:'Place: click the ground to drop the chosen model. Drag still orbits.',select:'Select: click an object or spawn you placed, then drag it. Q / E rotate, [ ] scale, Delete removes, Ctrl+D duplicates. Click a game object to copy it in.',spawn:'Spawn: choose a type below, then click the ground. Each click adds one; players face the way the camera faces.'};
function setMode(m){mode=mode===m?null:m;if(mode&&hooks.toolOff)hooks.toolOff();document.querySelectorAll('.seg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===mode));cv.classList.toggle('build',!!mode);$('builderHint').textContent=HINTS[mode]||'Pick a model and choose Place, then click the ground.';$('spawnKindRow').hidden=mode!=='spawn';if(mode!=='select')hidePick();}
document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
function ray(e){const r=cv.getBoundingClientRect(),nx=(e.clientX-r.left)/r.width*2-1,ny=1-(e.clientY-r.top)/r.height*2,{e:o}=viewProj(),t=cam.target,f=norm([t[0]-o[0],t[1]-o[1],t[2]-o[2]]),s=norm(cross(f,[0,1,0])),u=cross(s,f),a=r.width/r.height,fy=1/Math.tan(.45);
 return {o,d:norm([f[0]+s[0]*nx*a/fy+u[0]*ny/fy,f[1]+s[1]*nx*a/fy+u[1]*ny/fy,f[2]+s[2]*nx*a/fy+u[2]*ny/fy])};}
function planeHit(e,y){const {o,d}=ray(e);if(Math.abs(d[1])<1e-6)return null;const t=(y-o[1])/d[1];return t>0?[o[0]+d[0]*t,y,o[2]+d[2]*t]:null;}
function groundHit(e){ // nearest intersection with the loaded ground (Moller-Trumbore)
 const {o,d}=ray(e);let best=Infinity;
 for(const c of chunks.values()){if(c.hit){const t=c.hit(o,d);if(t<best)best=t;continue;}const p=c.pos,ix=c.idx;for(let i=0;i<ix.length;i+=3){const a=ix[i]*3,b=ix[i+1]*3,q=ix[i+2]*3,e1=[p[b]-p[a],p[b+1]-p[a+1],p[b+2]-p[a+2]],e2=[p[q]-p[a],p[q+1]-p[a+1],p[q+2]-p[a+2]],h=cross(d,e2),det=dot(e1,h);if(Math.abs(det)<1e-9)continue;const inv=1/det,s=[o[0]-p[a],o[1]-p[a+1],o[2]-p[a+2]],uu=dot(s,h)*inv;if(uu<0||uu>1)continue;const qq=cross(s,e1),vv=dot(d,qq)*inv;if(vv<0||uu+vv>1)continue;const tt=dot(e2,qq)*inv;if(tt>0&&tt<best)best=tt;}}
 return best<Infinity?[o[0]+d[0]*best,o[1]+d[1]*best,o[2]+d[2]*best]:null;}
const snapV=v=>{const s=+$('snap').value;return s?Math.round(v/s)*s:v;};
function alignToGround(o){const n=normalAt(o.pos[0],o.pos[2]),y=o.rot[0],f=[Math.sin(y),0,Math.cos(y)],r=[Math.cos(y),0,-Math.sin(y)],nl=[dot(n,r),n[1],dot(n,f)];o.rot[1]=+Math.atan2(nl[2],nl[1]).toFixed(4);o.rot[2]=+(-Math.asin(Math.max(-1,Math.min(1,nl[0])))).toFixed(4);}
function buildClick(e){
 if(!chunks.size){if(mode)status('Fly somewhere on the map first.',true);return;}
 const hit=groundHit(e);if(!hit){if(mode)status('Click on the ground.',true);return;}
 if(!mode){inspect(hit);return;}
 const x=snapV(hit[0]),z=snapV(hit[2]),y=heightAt(x,z)??hit[1],at=[x,y,z].map(v=>+v.toFixed(3));
 if(mode==='place'){const actor=$('actorList').value;if(!actor){status('Pick a model to place first.',true);return;}
  change(()=>{const o={actor,pos:at,rot:[+((cam.yaw+Math.PI)%(2*Math.PI)).toFixed(4),0,0],scale:[1,1,1]};if($('alignGround').checked)alignToGround(o);room.objects.push(o);selected=room.objects.length-1;selSpawn=-1;});status(`Placed ${actor.replace('.adr','')}.`);}
 else if(mode==='select'){let near=-1,best=Infinity,nearSpawn=-1,bestSpawn=Infinity;
  room.objects.forEach((o,i)=>{const d=Math.hypot(o.pos[0]-hit[0],o.pos[2]-hit[2]),reach=Math.max(3,(meshRadius(o.actor)||2)*o.scale[0]);if(d<reach&&d<best){best=d;near=i;}});
  room.spawns.forEach((s,i)=>{const d=Math.hypot(s.pos[0]-hit[0],s.pos[2]-hit[2]);if(d<3&&d<bestSpawn){bestSpawn=d;nearSpawn=i;}});
  if(nearSpawn>=0&&bestSpawn<=best){selSpawn=nearSpawn;selected=-1;hidePick();queueRoom();}
  else if(near>=0){selected=near;selSpawn=-1;hidePick();queueRoom();}else{selected=-1;selSpawn=-1;queueRoom();pickScene(hit);}}
 else if(mode==='spawn'){const [kind,detail='']=$('spawnKind').value.split(':');change(()=>{room.spawns.push({kind,detail,pos:at,yaw:+cam.yaw.toFixed(4)});selSpawn=room.spawns.length-1;selected=-1;});status(`${SPAWN_KINDS[kind][0]} spawn added${detail?' ('+detail+')':''}.`);}}
// clicking the world with no tool: what spawn or object is there
function inspect(hit){let best=null,bd=6;
 for(const c of chunks.values())for(const [k,rows] of Object.entries(c.markerRows||{})){if(!layerOn[k])continue;for(const r of rows){const d=Math.hypot(r[0]-hit[0],r[2]-hit[2]);if(d<bd){bd=d;best={k,r};}}}
 if(!best){pickScene(hit,true);return;}const info=layerInfo.find(l=>l.key===best.k)||{label:best.k,colour:'#fff'},r=best.r,kind=LAYER_KIND[best.k];scenePick=null;
 $('pick').innerHTML=`<b><i class="sw" style="background:${info.colour}"></i>${info.label.replace(/s(?= \(|$)/,'')}</b><small>${r[4]} · at ${r.slice(0,3).map(v=>v.toFixed(1)).join(', ')} · facing ${Math.round(r[3]*180/Math.PI)}°</small><div class="row">${kind?'<button id="pickSpawn" class="primary">Add to my room as a spawn</button>':''}<button id="pickClose">✕</button></div>`;
 $('pick').hidden=false;$('pickClose').onclick=hidePick;if(kind)$('pickSpawn').onclick=()=>{change(()=>{room.spawns.push({kind,detail:kind==='vehicle'?String(r[4]).split(' - ').pop():'',pos:r.slice(0,3),yaw:r[3]});selSpawn=room.spawns.length-1;});hidePick();status('Added to your room. Open the Room builder to move or save it.');};}
// the game's own objects near a click, so one can be copied into the room
function pickScene(hit,quiet){let best=null,bd=8;for(const c of chunks.values())for(const g of c.objects)for(const r of g.instances){const d=Math.hypot(r[0]-hit[0],r[2]-hit[2]);if(d<bd){bd=d;best={actor:g.actor,row:r};}}
 if(!best){hidePick();if(!quiet)status('Nothing there. Click nearer an object.',true);return;}scenePick=best;const r=best.row;
 $('pick').innerHTML=`<b>${best.actor.replace('.adr','')}</b><small>Game object at ${r.slice(0,3).map(v=>v.toFixed(1)).join(', ')} · yaw ${Math.round(r[3]*180/Math.PI)}°</small><div class="row"><button id="pickCopy" class="primary">Copy into room (C)</button><button id="pickUse">Use as model to place</button><button id="pickClose">✕</button></div>`;
 $('pick').hidden=false;$('pickCopy').onclick=copyPick;$('pickUse').onclick=()=>{$('builder').open=true;$('actorSearch').value=best.actor.replace('.adr','');renderActors();$('actorList').value=best.actor;setMode('place');};$('pickClose').onclick=hidePick;}
function hidePick(){scenePick=null;$('pick').hidden=true;}
function copyPick(){if(!scenePick)return;const r=scenePick.row;change(()=>{room.objects.push({actor:scenePick.actor,pos:r.slice(0,3),rot:r.slice(3,6),scale:r.slice(6,9)});selected=room.objects.length-1;selSpawn=-1;});hidePick();status('Copied into your room. Drag it to move it.');}
function duplicate(){
 if(selSpawn>=0){const s=room.spawns[selSpawn];change(()=>{const c=JSON.parse(JSON.stringify(s));c.pos[0]=+(c.pos[0]+2).toFixed(3);c.pos[1]=heightAt(c.pos[0],c.pos[2])??c.pos[1];room.spawns.push(c);selSpawn=room.spawns.length-1;});status('Duplicated.');return;}
 if(selected<0)return;const o=room.objects[selected];change(()=>{const c=JSON.parse(JSON.stringify(o));c.pos[0]=+(c.pos[0]+2).toFixed(3);c.pos[1]=heightAt(c.pos[0],c.pos[2])??c.pos[1];room.objects.push(c);selected=room.objects.length-1;});status('Duplicated.');}
function undo(){if(!history.length)return;future.push(snapshot());restore(history.pop());status('Undone.');}
function redo(){if(!future.length)return;history.push(snapshot());restore(future.pop());status('Redone.');}
$('undo').onclick=undo;$('redo').onclick=redo;$('dup').onclick=duplicate;
// copy the game's spawns that are switched on and loaded into the room
$('importSpawns').onclick=()=>{const add=[];for(const c of chunks.values())for(const [k,rows] of Object.entries(c.markerRows||{})){const kind=LAYER_KIND[k];if(!kind||!layerOn[k])continue;for(const r of rows)add.push({kind,detail:kind==='vehicle'?String(r[4]).split(' - ').pop():'',pos:r.slice(0,3),yaw:r[3]});}
 if(!add.length){status('No spawns in view. Load an area and switch on some spawn layers first.',true);return;}
 if(room.spawns.length+add.length>5000){status(`That is ${add.length} spawns; a room holds up to 5000. Switch off a layer (Gear is the largest).`,true);return;}
 if(!confirm(`Copy ${add.length} spawn points from the loaded area into this room?`))return;change(()=>{room.spawns.push(...add);});status(`Copied ${add.length} spawn points into your room.`);};
$('clearSpawns').onclick=()=>{if(!room.spawns.length||!confirm(`Remove all ${room.spawns.length} spawn points from this room?`))return;change(()=>{room.spawns=[];selSpawn=-1;});};
function buildKey(e){const k=e.key.toLowerCase();
 if(e.ctrlKey||e.metaKey){if(k==='z'){e.preventDefault();e.shiftKey?redo():undo();}else if(k==='y'){e.preventDefault();redo();}else if(k==='d'){e.preventDefault();duplicate();}return;}
 if($('builder').open&&['1','2','3'].includes(k)){setMode(['place','select','spawn'][+k-1]);return;}
 if(k==='c'&&scenePick){copyPick();return;}
 if(k==='escape'){hidePick();if(selected>=0||selSpawn>=0){selected=-1;selSpawn=-1;queueRoom();}else setMode(null);return;}
 const step=+$('rotStep').value*Math.PI/180;
 if(selSpawn>=0&&room.spawns[selSpawn]){const s=room.spawns[selSpawn];
  if(k==='q')change(()=>{s.yaw=+((s.yaw||0)-step).toFixed(4);});else if(k==='e')change(()=>{s.yaw=+((s.yaw||0)+step).toFixed(4);});
  else if(k==='delete'||k==='backspace')change(()=>{room.spawns.splice(selSpawn,1);selSpawn=-1;});else return;e.preventDefault();return;}
 if(selected<0||!room.objects[selected])return;
 if(k==='q')change(()=>{room.objects[selected].rot[0]=+(room.objects[selected].rot[0]-step).toFixed(4);});
 else if(k==='e')change(()=>{room.objects[selected].rot[0]=+(room.objects[selected].rot[0]+step).toFixed(4);});
 else if(k===']')change(()=>{room.objects[selected].scale=room.objects[selected].scale.map(v=>+(v*1.1).toFixed(3));});
 else if(k==='[')change(()=>{room.objects[selected].scale=room.objects[selected].scale.map(v=>+(v/1.1).toFixed(3));});
 else if(k==='delete'||k==='backspace')change(()=>{room.objects.splice(selected,1);selected=-1;});
 else return;e.preventDefault();}
function renderActors(){const q=$('actorSearch').value.trim().toLowerCase();const rows=actors.filter(a=>!q||a.actor.toLowerCase().includes(q)).slice(0,400);
 $('actorList').innerHTML=rows.map(a=>`<option value="${a.actor}">${a.actor.replace('.adr','')} (${a.count})</option>`).join('');}
$('actorSearch').oninput=renderActors;
async function loadActors(){try{actors=(await api('/api/terrain/actors?zone='+zone)).actors;renderActors();}catch(err){status(err.message,true);}}
async function refreshRooms(){try{const r=await api('/api/terrain/rooms');token=r.token;$('roomList').innerHTML='<option value="">Open a saved room…</option>'+r.rooms.map(x=>`<option value="${x.name}">${x.name} · ${x.zone} · ${x.objects} objects · ${x.spawns||0} spawns</option>`).join('');}catch(err){status(err.message,true);}}
$('roomList').onchange=async()=>{const n=$('roomList').value;if(!n)return;try{const r=await api('/api/terrain/room?name='+encodeURIComponent(n));
  if(r.zone!==zone){$('zone').value=r.zone;await openZone(r.zone);}
  room=normaliseRoom({name:r.name,zone:r.zone,spawn:r.spawn,objects:r.objects,spawns:r.spawns});selected=-1;selSpawn=-1;history=[];future=[];$('roomName').value=r.name;
  const at=room.spawns[0]?.pos||r.objects[0]?.pos;if(at)flyTo(at[0],at[2]);queueRoom();status(`Opened room ${r.name}.`);}catch(err){status(err.message,true);}};
$('roomSave').onclick=async()=>{const name=$('roomName').value.trim();if(!name){status('Name the room first.',true);$('roomName').focus();return;}
 // the first player spawn is also written as `spawn`, which older readers use
 const first=room.spawns.find(s=>s.kind==='player'),body={...room,name,zone,spawn:first?[...first.pos,first.yaw||0]:null};
 try{const r=await fetch('/api/terrain/rooms',{method:'POST',headers:{'content-type':'application/json','X-Studio-Token':token},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);room.name=d.room.name;status(`Saved ${d.room.objects.length} objects and ${d.room.spawns.length} spawns to ${d.saved}`);refreshRooms();}catch(err){status('Save failed: '+err.message,true);}};
$('roomNew').onclick=()=>{room={name:'',zone,spawn:null,objects:[],spawns:[]};selected=-1;selSpawn=-1;history=[];future=[];$('roomName').value='';queueRoom();status('New empty room.');};
$('roomDelete').onclick=async()=>{const name=$('roomName').value.trim();if(!name||!confirm(`Delete room "${name}"?`))return;
 try{const r=await fetch('/api/terrain/rooms/delete',{method:'POST',headers:{'content-type':'application/json','X-Studio-Token':token},body:JSON.stringify({name})});if(!r.ok)throw Error((await r.json()).error);$('roomNew').click();refreshRooms();status(`Deleted ${name}.`);}catch(err){status(err.message,true);}};
$('builder').addEventListener('toggle',()=>{if(!$('builder').open){setMode(null);return;}if(!actors.length)loadActors();refreshRooms();roomInfo();});
window.TerrainCore={gl,$,api,status,b64,buffer,program,loc,G,chunks,state,cam,eye,viewProj,ray,makeSet,freeSet,drawSet,geometry,meshFrom,prism,cone,octa,PR,hooks,
 zone:()=>zone,game:()=>game,setGame,setSnow,palette:()=>ecoInfo,overview:()=>overview,openZone,unloadAll,unloadChunk,enqueue,drawMap,info,flyTo,heightAt,zoneSample,
 setMode:m=>{if(mode)setMode(mode);if(m)setMode(m);},mode:()=>mode,builderOpen:()=>$('builder').open,empty:v=>{$('empty').hidden=!v;}};
(async()=>{try{const g=await api('/api/terrain/games');game=g.current;$('game').innerHTML=g.games.map(x=>`<option value="${x.key}" ${x.available?'':'disabled'}>${x.label}${x.available?'':' (not found)'}</option>`).join('');$('game').value=game;
 await loadZones('Z1');roomInfo();}catch(err){status(err.message,true);}})();
})();
