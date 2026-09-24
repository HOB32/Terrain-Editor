/* Terrain [beta] · Christmas: snow, falling snow and Christmas lights for the KotK client's
   own Z2, built into the overlay pack Assets_287 (season_build.py) plus the launcher's
   server season (launcher/server/season.js). */
'use strict';
(()=>{
const T=window.TerrainCore;if(!T)return;
const {gl,$,api,status}=T;
const RGB={red:[1,.18,.15],green:[.2,1,.3],gold:[1,.75,.25],blue:[.3,.55,1],warm:[1,.88,.66],white:[.9,.95,1],pink:[1,.4,.8],purple:[.7,.4,1]};
const SCHEME_COLOURS={multi:['red','green','gold','blue'],candy:['red','white'],classic:['red','green','gold'],warm:['warm'],white:['white'],red:['red'],green:['green'],gold:['gold'],blue:['blue'],frost:['white','blue']};
const HINTS={string:'Click where the string starts, then where it ends. Esc cancels.',tree:'Click the ground where the tree of lights stands.',lamp:'Click where the glow goes; it sits 2.5 m above the ground.',erase:'Click next to a decoration to remove it.'};
let active=false,project=null,token='',tool=null,first=null,cursor=null,dirty=false,busy=false;

// ------------------------------------------------------------ drawing
const prog=T.program(`#version 300 es
in vec3 aPos;uniform mat4 uVP;void main(){gl_Position=uVP*vec4(aPos,1.);gl_PointSize=6.;}`,`#version 300 es
precision highp float;uniform vec4 uCol;out vec4 o;void main(){o=uCol;}`);
const L=T.loc(prog,['aPos','uVP','uCol']);
const vao=gl.createVertexArray(),buf=gl.createBuffer();
gl.bindVertexArray(vao);gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.enableVertexAttribArray(L.aPos);gl.vertexAttribPointer(L.aPos,3,gl.FLOAT,false,0,0);gl.bindVertexArray(null);
function lines(pts,mode,col){if(pts.length<3)return;gl.bindVertexArray(vao);gl.bindBuffer(gl.ARRAY_BUFFER,buf);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(pts),gl.DYNAMIC_DRAW);gl.uniform4fv(L.uCol,col);gl.drawArrays(mode,0,pts.length/3);}
const colourOf=d=>RGB[(SCHEME_COLOURS[d.colour]||SCHEME_COLOURS.multi)[0]]||RGB.gold;
function stringPts(d){const a=d.a,b=d.b,len=Math.hypot(b[0]-a[0],b[1]-a[1],b[2]-a[2]),sag=(d.sag??.35)*len*.08,pts=[];
 for(let k=0;k<=16;k++){const t=k/16;pts.push(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t-sag*4*t*(1-t),a[2]+(b[2]-a[2])*t);}return pts;}
function draw(m){
 if(!active||!project)return;const eye=T.cam.target,far=2500;
 gl.useProgram(prog);gl.uniformMatrix4fv(L.uVP,false,m);
 for(const d of project.decor){const p=d.a||d.at;if(!p||Math.abs(p[0]-eye[0])>far||Math.abs(p[2]-eye[2])>far)continue;const c=colourOf(d);
  if(d.kind==='string'){const pts=stringPts(d),cs=(SCHEME_COLOURS[d.colour]||SCHEME_COLOURS.multi).map(n=>RGB[n]||RGB.gold);
   for(let k=0;k<16;k++)lines(pts.slice(k*3,k*3+6),gl.LINES,[...cs[k%cs.length],1]);}
  else if(d.kind==='tree'){const h=d.height||7,r=d.radius||2.4,[x,y,z]=d.at,pts=[];for(let k=0;k<=24;k++){const t=k/24*Math.PI*2;pts.push(x+Math.cos(t)*r,y,z+Math.sin(t)*r);}
   lines(pts,gl.LINE_STRIP,[...c,1]);const sp=[];for(let k=0;k<6;k++){const t=k/6*Math.PI*2;sp.push(x+Math.cos(t)*r,y,z+Math.sin(t)*r,x,y+h,z);}lines(sp,gl.LINES,[...c,.8]);lines([x,y+h+.3,z],gl.POINTS,[1,.8,.3,1]);}
  else if(d.kind==='campfire'){const [x,y,z]=d.at,pts=[];for(let k=0;k<=20;k++){const t=k/20*Math.PI*2;pts.push(x+Math.cos(t)*7.5,y+.2,z+Math.sin(t)*7.5);}
   lines(pts,gl.LINE_STRIP,[1,.55,.15,.9]);lines([x,y+.2,z,x,y+1.6,z],gl.LINES,[1,.45,.1,1]);lines([x,y+1.6,z],gl.POINTS,[1,.7,.2,1]);}
  else if(d.kind==='fireworks'){const [x,y,z]=d.at,t=performance.now()/1000,sp=[];
   for(let k=0;k<5;k++){const a=k*2.4+Math.floor(t/1.3+k)*1.7,r=(d.radius||450)*.35*((k*37%10)/10+.2),bx=x+Math.cos(a)*r,bz=z+Math.sin(a)*r,by=y+110+k*12,ph=(t/1.3+k*.37)%1;
    for(let j=0;j<10;j++){const b=j/10*Math.PI*2,s=18*ph;sp.push(bx,by,bz,bx+Math.cos(b)*s,by+Math.sin(b)*s*.7,bz+Math.sin(b)*s*.5);}}
   lines(sp,gl.LINES,[1,.85,.35,1]);}
  else{const [x,y,z]=d.at;lines([x,y-2.5,z,x,y,z],gl.LINES,[.8,.8,.8,1]);lines([x,y,z],gl.POINTS,[...c,1]);}}
 if(tool==='string'&&first&&cursor)lines([first[0],first[1],first[2],cursor[0],cursor[1]+stringLift(),cursor[2]],gl.LINES,[1,1,1,1]);
 if(tool&&cursor)lines([cursor[0],cursor[1],cursor[2],cursor[0],cursor[1]+3,cursor[2]],gl.LINES,[1,.72,.25,1]);
 gl.useProgram(null);}
function mapDraw(mc,mapXY){
 if(!active||!project)return;mc.save();
 for(const d of project.decor){const c=colourOf(d).map(v=>Math.round(v*255)).join(',');mc.fillStyle=mc.strokeStyle=`rgb(${c})`;
  if(d.kind==='string'){const [x,y]=mapXY(d.a[0],d.a[2]),[x2,y2]=mapXY(d.b[0],d.b[2]);mc.lineWidth=1.5;mc.beginPath();mc.moveTo(x,y);mc.lineTo(x2,y2);mc.stroke();}
  else if(d.kind==='fireworks'){const [x,y]=mapXY(d.at[0],d.at[2]),[x2]=mapXY(d.at[0]+(d.radius||450),d.at[2]);mc.strokeStyle='rgba(255,210,90,.55)';mc.setLineDash([3,3]);mc.lineWidth=1;mc.beginPath();mc.arc(x,y,Math.max(3,x2-x),0,7);mc.stroke();mc.setLineDash([]);}
  else if(d.kind==='campfire'){const [x,y]=mapXY(d.at[0],d.at[2]);mc.fillStyle='#ff8a2a';mc.fillRect(x-1,y-1,2,2);}
  else{const [x,y]=mapXY(d.at[0],d.at[2]);mc.beginPath();mc.arc(x,y,d.kind==='tree'?(d.height>10?4:2.5):1.8,0,7);mc.fill();}}
 mc.restore();}

// ------------------------------------------------------------ picking the ground
function pick(e){const {o,d}=T.ray(e);let prev=null;
 for(let t=1;t<6000;t+=t<200?1:t<1000?4:12){const x=o[0]+d[0]*t,y=o[1]+d[1]*t,z=o[2]+d[2]*t,h=T.heightAt(x,z);
  if(h!=null&&y<=h){if(!prev)return [x,h,z];let a=prev,b=t;for(let i=0;i<12;i++){const m=(a+b)/2,mx=o[0]+d[0]*m,mz=o[2]+d[2]*m,mh=T.heightAt(mx,mz);if(mh!=null&&o[1]+d[1]*m<=mh)b=m;else a=m;}
   const px=o[0]+d[0]*b,pz=o[2]+d[2]*b;return [px,T.heightAt(px,pz)??y,pz];}
  prev=t;}return null;}
const stringLift=()=>5;   // strings hang 5 m up: eaves and lamp posts
function r2(v){return Math.round(v*100)/100;}

// ------------------------------------------------------------ project <-> controls
const SL=[['snGather',v=>`${v} min`],['snFog',v=>`${v}%`],['snCloud',v=>`${v}%`],['snWind',v=>(v/10).toFixed(1)],['snFlake',v=>`${v}%`],
 ['snTime',v=>`${String(Math.floor(v)).padStart(2,'0')}:${String(Math.round(v%1*60)).padStart(2,'0')}`],['snRange',v=>`${v} m`],['snBulb',v=>`${v}%`],['snBright',v=>`${v}%`]];
for(const [id,fmt] of SL){const out=$(id+'Out'),upd=()=>{out.textContent=fmt(+$(id).value);};$(id).addEventListener('input',()=>{upd();dirty=true;});upd();}
for(const id of ['snCover','snFall','snStart','snEnd','snMood','snLightsOn','snReal','snNight','snGround','snFlora','snFireworks'])$(id).addEventListener('change',()=>{dirty=true;});
$('snPreview').addEventListener('change',()=>previewSnow());
// Snow in the 3D view while this tab is open, thawed round the lit campfires nearest the view.
function previewSnow(){if(!active||!project||!$('snPreview').checked){T.setSnow(0,[]);return;}
 const t=T.cam.target,near=project.decor.filter(d=>d.kind==='campfire').map(d=>[d.at[0],d.at[1],d.at[2],7.5])
  .sort((a,b)=>Math.hypot(a[0]-t[0],a[2]-t[2])-Math.hypot(b[0]-t[0],b[2]-t[2])).slice(0,24);
 T.setSnow($('snGround').checked||$('snCover').checked?1:0,near);}
function toControls(p){const s=p.snow,l=p.lights,m=p.lighting;
 $('snCover').checked=!!(s.shader&&s.cover);$('snFall').value=s.snowfall;$('snGather').value=s.gather_minutes;$('snStart').value=s.start_temp;$('snEnd').value=s.end_temp;
 $('snGround').checked=s.ground!==false;$('snFlora').checked=s.flora!==false;$('snFireworks').value=s.fireworks||'normal';
 $('snFog').value=Math.round(s.fog*100);$('snCloud').value=Math.round(s.overcast*100);$('snWind').value=Math.round(s.wind*10);$('snFlake').value=Math.round(s.flake_size*100);
 $('snMood').value=m.mood;$('snTime').value=m.time;$('snLightsOn').checked=!!m.lights_always_on;
 $('snRange').value=l.range;$('snBulb').value=Math.round(l.bulb_size*100);$('snBright').value=Math.round(l.brightness*100);$('snReal').checked=!!l.real_lights;$('snNight').checked=!!l.night_only;
 for(const [id] of SL)$(id).dispatchEvent(new Event('input'));dirty=false;}
function fromControls(){const p=project;
 Object.assign(p.snow,{cover:$('snCover').checked,shader:$('snCover').checked,snowfall:$('snFall').value,gather_minutes:+$('snGather').value,start_temp:+$('snStart').value,end_temp:+$('snEnd').value,
  fog:$('snFog').value/100,overcast:$('snCloud').value/100,wind:$('snWind').value/10,flake_size:$('snFlake').value/100,
  ground:$('snGround').checked,flora:$('snFlora').checked,fireworks:$('snFireworks').value});
 Object.assign(p.lighting,{mood:$('snMood').value,time:+$('snTime').value,lights_always_on:$('snLightsOn').checked});
 Object.assign(p.lights,{range:+$('snRange').value,bulb_size:$('snBulb').value/100,brightness:$('snBright').value/100,real_lights:$('snReal').checked,night_only:$('snNight').checked});
 return p;}
function info(extra){if(!project)return;const n={string:0,tree:0,lamp:0,campfire:0,fireworks:0};let lights=0;
 for(const d of project.decor){n[d.kind]=(n[d.kind]||0)+1;if(d.light!==false&&d.kind!=='campfire'&&d.kind!=='fireworks')lights++;}
 const near=project.decor.filter(d=>{const p=d.a||d.at,t=T.cam.target;return Math.hypot(p[0]-t[0],p[2]-t[2])<(+$('snRange').value||180);}).length;
 $('seasonInfo').innerHTML=`<b>${project.decor.length}</b> decorations: ${n.string} strings, ${n.tree} trees, ${n.lamp} lamps, ${n.campfire} lit campfires, ${n.fireworks} fireworks towns · ${near} within draw distance of the view`+
  `${$('snReal').checked?` · ${Math.min(near,lights)} real lights there`:''}${dirty?' · <b>unsaved</b>':''}${extra?`<br>${extra}`:''}`;}
function stateLine(st){const s=st||{};$('seasonState').innerHTML=s.installed?`<b>Installed</b> as ${(s.packs||[]).join(', ')} in the KotK client${s.server_enabled?' and the launcher\'s server':''}. Restart the servers and the game to see changes.`:
 (s.error?`<span style="color:#ffb4a8">${s.error}</span>`:'Not installed. Christmas edits the KotK depot\'s own Z2 in place, as overlay packs straight after the reserved Assets_261 (Assets_262-264, plus 287 for the few textures only it can replace); nothing the game ships is changed.');}

async function load(){const r=await api('/api/terrain/season');token=r.token;project=r.project;
 $('snScheme').innerHTML=r.schemes.map(s=>`<option value="${s}">${s[0].toUpperCase()+s.slice(1)}</option>`).join('');$('snScheme').value='multi';
 toControls(project);stateLine(r.status);info();T.drawMap();}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json','X-Studio-Token':token},body:JSON.stringify(body)});
 const d=await r.json().catch(()=>({error:`Unreadable response (${r.status})`}));if(!r.ok)throw Error(d.error||r.statusText);return d;}

// ------------------------------------------------------------ tools
function setTool(t){tool=tool===t?null:t;first=null;document.querySelectorAll('[data-stool]').forEach(b=>b.classList.toggle('on',b.dataset.stool===tool));
 $('snToolHint').textContent=tool?HINTS[tool]:'Pick a tool, then click the ground in the 3D view. Right-drag orbits.';if(tool)T.setMode(null);}
document.querySelectorAll('[data-stool]').forEach(b=>b.onclick=()=>setTool(b.dataset.stool));
function base(){return {colour:$('snScheme').value,twinkle:$('snTwinkle').checked};}
function place(h){
 if(tool==='string'){const p=[r2(h[0]),r2(h[1]+stringLift()),r2(h[2])];if(!first){first=p;status('Now click where the string ends.');return;}
  if(Math.hypot(p[0]-first[0],p[2]-first[2])<1){status('Too short; click further away.',true);return;}
  project.decor.push({kind:'string',a:first,b:p,sag:.35,...base()});first=p;status('String added. Keep clicking to chain another from here, or Esc to stop.');}
 else if(tool==='tree'){const hh=+$('snTreeH').value;project.decor.push({kind:'tree',at:[r2(h[0]),r2(h[1]),r2(h[2])],height:hh,radius:r2(hh*.33),...base()});status('Tree of lights added.');}
 else if(tool==='lamp'){project.decor.push({kind:'lamp',at:[r2(h[0]),r2(h[1]+2.5),r2(h[2])],...base(),colour:$('snScheme').value==='multi'?'warm':$('snScheme').value});status('Lamp added.');}
 else if(tool==='erase'){let best=-1,bd=8;project.decor.forEach((d,i)=>{const pts=d.kind==='string'?[d.a,d.b,[(d.a[0]+d.b[0])/2,0,(d.a[2]+d.b[2])/2]]:[d.at];
   for(const p of pts){const dd=Math.hypot(p[0]-h[0],p[2]-h[2]);if(dd<bd){bd=dd;best=i;}}});
  if(best<0){status('Nothing within 8 m of the click.',true);return;}project.decor.splice(best,1);status('Removed.');}
 dirty=true;info();T.drawMap();}

// ------------------------------------------------------------ hooks (chained with the Create tab's)
const H=T.hooks,prev={...H};
H.active=()=>active?!!tool:(prev.active?prev.active():false);
H.frozen=()=>active?false:(prev.frozen?prev.frozen():false);
H.down=e=>{if(!active)return prev.down?prev.down(e):false;if(!tool||e.button!==0||e.shiftKey)return false;
 const h=pick(e);if(!h){status('Point at loaded ground (fly there first so it loads).',true);return true;}place(h);return true;};
H.move=(e,dragging)=>{if(!active)return prev.move?prev.move(e,dragging):false;cursor=tool?pick(e):null;return false;};
H.up=e=>active?false:(prev.up?prev.up(e):false);
H.key=e=>{if(!active)return prev.key?prev.key(e):false;const k=e.key.toLowerCase();
 if(/^[1-4]$/.test(k)&&!e.ctrlKey){setTool(['string','tree','lamp','erase'][+k-1]);return true;}
 if(k==='escape'&&tool){if(first){first=null;status('String stopped.');}else setTool(tool);return true;}return false;};
H.draw=(m,e,o)=>{if(prev.draw)prev.draw(m,e,o);draw(m);};
H.map=(mc,mapXY)=>{if(prev.map)prev.map(mc,mapXY);mapDraw(mc,mapXY);};
H.toolOff=()=>{if(active&&tool)setTool(tool);else if(prev.toolOff)prev.toolOff();};

// ------------------------------------------------------------ tab
async function openSeason(){
 $('tabExploreBtn').onclick();   // leaves the Create tab cleanly (its own setTab)
 active=true;$('tabExplore').hidden=true;$('tabSeason').hidden=false;$('exploreNote').hidden=true;
 for(const [id,on] of [['tabExploreBtn',false],['tabCreateBtn',false],['tabSeasonBtn',true]]){$(id).classList.toggle('on',on);$(id).setAttribute('aria-selected',String(on));}
 $('seasonMapSlot').after($('map'));
 try{if(T.game()!=='kotk'){status('Switching to the KotK depot…');await T.setGame('kotk','Z2');}
  if(T.zone()!=='Z2'){$('zone').value='Z2';await T.openZone('Z2');}
  if(!project)await load();else{info();T.drawMap();}
  previewSnow();
  status('Christmas: fly to a town, pick a tool and click the ground, or Decorate automatically. Build & install when ready.');}
 catch(err){status(err.message,true);}}
function closeSeason(){if(!active)return;active=false;T.setSnow(0,[]);if(tool)setTool(tool);first=null;cursor=null;$('tabSeason').hidden=true;$('tabSeasonBtn').classList.remove('on');$('tabSeasonBtn').setAttribute('aria-selected','false');}
const exploreClick=$('tabExploreBtn').onclick,createClick=$('tabCreateBtn').onclick;
$('tabExploreBtn').onclick=()=>{closeSeason();exploreClick();};
$('tabCreateBtn').onclick=()=>{closeSeason();createClick();};
$('tabSeasonBtn').onclick=openSeason;

// ------------------------------------------------------------ actions
async function save(){fromControls();const d=await post('/api/terrain/season',project);project=d.project;dirty=false;info();return d;}
$('snSave').onclick=async()=>{try{const d=await save();status(`Saved ${d.decor} decorations to ${d.saved}.`);}catch(err){status('Save failed: '+err.message,true);}};
$('snBuild').onclick=async()=>{if(busy)return;busy=true;$('snBuild').disabled=true;
 try{fromControls();status('Building the Christmas pack…');const d=await post('/api/terrain/season/build',{project,install:$('snInstall').checked});dirty=false;
  const r=d.report;stateLine(d.status);
  info(`Built ${d.packs.length} packs, ${(d.bytes/1048576).toFixed(1)} MB: ${r.decor} lights (${r.bulbs.toLocaleString()} bulbs, ${r.real_lights} real lights), ${r.campfires||0} campfires, ${r.fireworks||0} fireworks towns${r.snowfall_effect?', falling snow':''}`+
   d.packs.map(p=>`<br>· <b>${p.pack}</b> ${p.feature}: ${p.files} files, ${(p.bytes/1048576).toFixed(1)} MB${p.searched_first?'':' <b>(warning: not searched before the shipped copies)</b>'}`).join('')+
   `${r.warnings.length?`<br>${r.warnings.join('<br>')}`:''}`);
  status(d.installed?'Christmas installed. Restart the launcher\'s servers and the game.':`Built; not installed (the packs are in ${d.built}).`);}
 catch(err){status('Build failed: '+err.message,true);}finally{busy=false;$('snBuild').disabled=false;}};
$('snRemove').onclick=async()=>{if(!confirm('Uninstall Christmas from the KotK client and the launcher\'s server? Your decorations stay saved.'))return;
 try{const d=await post('/api/terrain/season/uninstall',{});stateLine({installed:false});status(d.removed.length?`Removed ${d.removed.length} file${d.removed.length===1?'':'s'}.`:'Christmas was not installed.');}catch(err){status(err.message,true);}};
async function decorate(around){if(busy)return;busy=true;
 try{status('Finding street lamps, trees and towns in Z2…');const t=T.cam.target,bounds=around?[t[0]-1000,t[2]-1000,t[0]+1000,t[2]+1000]:null;
  const d=await post('/api/terrain/season/decorate',{bounds,options:{streets:$('adStreets').checked,trees:$('adTrees').checked,towns:$('adTowns').checked,fences:$('adFences').checked,
   campfires:$('adCampfires').checked,fireworks:$('adFireworks').checked,
   max:+$('adMax').value,scheme:$('snScheme').value,twinkle:$('snTwinkle').checked}});
  // replace earlier automatic decorations in the same region, keep hand-placed ones
  const inRegion=p=>!bounds||(p[0]>=bounds[0]&&p[0]<bounds[2]&&p[2]>=bounds[1]&&p[2]<bounds[3]);
  project.decor=project.decor.filter(x=>!x.auto||!inRegion(x.a||x.at)).concat(d.decor);dirty=true;info();T.drawMap();
  status(`Added ${d.decor.length} decorations${around?' around the view':' across Z2'}. Save or Build & install to keep them.`);}
 catch(err){status(err.message,true);}finally{busy=false;}}
$('adView').onclick=()=>decorate(true);$('adAll').onclick=()=>decorate(false);
$('adClearAuto').onclick=()=>{if(!project)return;const n=project.decor.length;project.decor=project.decor.filter(d=>!d.auto);dirty=true;info();T.drawMap();status(`Cleared ${n-project.decor.length} automatic decorations.`);};
$('adClearAll').onclick=()=>{if(!project||!project.decor.length||!confirm(`Remove all ${project.decor.length} decorations?`))return;project.decor=[];dirty=true;info();T.drawMap();status('All decorations cleared.');};
setInterval(()=>{if(active){info();previewSnow();}},1000);
})();
