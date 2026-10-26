/* PANIC ENGINE live view - DOM-free core: walking graph, route finder, manual avatar stepping. Unit-tested in Node. */
function makeCore(D){
"use strict";
const G=D.graph,NN=G.lon.length,EN=G.eu.length;
const cosLat=Math.cos(D.view.lat*Math.PI/180),MD=111320,WALK=1.4;
D.closures.sort((a,b)=>a[1]-b[1]);
/* ---------- geometry / graph ---------- */
function dM(lo1,la1,lo2,la2){return Math.hypot((lo2-lo1)*cosLat*MD,(la2-la1)*MD);}
const adjStart=new Int32Array(NN+1);for(let e=0;e<EN;e++)adjStart[G.eu[e]+1]++;for(let i=0;i<NN;i++)adjStart[i+1]+=adjStart[i];
const fillp=adjStart.slice(0,NN),adjTo=new Int32Array(EN),adjLen=new Float32Array(EN),adjRoad=new Int32Array(EN);
for(let e=0;e<EN;e++){const k=fillp[G.eu[e]]++;adjTo[k]=G.ev[e];adjLen[k]=G.el[e];adjRoad[k]=G.er[e];}
const CELL=0.002,grid=new Map();
for(let i=0;i<NN;i++){const key=Math.floor(G.lon[i]/CELL)+','+Math.floor(G.lat[i]/CELL);let a=grid.get(key);if(!a)grid.set(key,a=[]);a.push(i);}
function nearestNode(lon,lat){const cx=Math.floor(lon/CELL),cy=Math.floor(lat/CELL);let best=-1,bd=1e18;
 for(let r=0;r<=8;r++){for(let dx=-r;dx<=r;dx++)for(let dy=-r;dy<=r;dy++){if(Math.max(Math.abs(dx),Math.abs(dy))!==r)continue;
  const a=grid.get((cx+dx)+','+(cy+dy));if(!a)continue;for(const i of a){const d=Math.pow((G.lon[i]-lon)*cosLat,2)+Math.pow(G.lat[i]-lat,2);if(d<bd){bd=d;best=i;}}}
  if(best>=0&&r>=1)break;}return best;}
let closedSet=new Set(),closedCount=-1;
function closedAt(x){let c=0;for(const cl of D.closures)if(cl[1]<=x)c++;if(c!==closedCount){closedSet=new Set();for(let i=0;i<c;i++)closedSet.add(D.closures[i][0]);closedCount=c;}return closedSet;}
function safeOpen(i,x){const s=D.safe[i];return s.lost===null||x<s.lost;}
/* binary heap Dijkstra to the nearest open safe stop (or a chosen one) */
function route(src,x,pref){const closed=closedAt(x),dist=new Float64Array(NN).fill(Infinity),prev=new Int32Array(NN).fill(-1),tg=new Map();
 D.safe.forEach((s,i)=>{if(safeOpen(i,x)&&(pref<0||pref===i))tg.set(s.node,i);});if(!tg.size||src<0)return null;
 const hd=[0],hn=[src];dist[src]=0;
 const push=(d,v)=>{let i=hd.length;hd.push(d);hn.push(v);while(i>0){const p=(i-1)>>1;if(hd[p]<=hd[i])break;[hd[p],hd[i]]=[hd[i],hd[p]];[hn[p],hn[i]]=[hn[i],hn[p]];i=p;}};
 const pop=()=>{const d=hd[0],v=hn[0],ld=hd.pop(),lv=hn.pop();if(hd.length){hd[0]=ld;hn[0]=lv;let i=0;for(;;){let l=2*i+1,r=l+1,m=i;if(l<hd.length&&hd[l]<hd[m])m=l;if(r<hd.length&&hd[r]<hd[m])m=r;if(m===i)break;[hd[m],hd[i]]=[hd[i],hd[m]];[hn[m],hn[i]]=[hn[i],hn[m]];i=m;}}return[d,v];};
 hd.length=0;hn.length=0;push(0,src);
 while(hd.length){const[d,u]=pop();if(d>dist[u])continue;
  if(tg.has(u)){const path=[];for(let v=u;v>=0;v=prev[v])path.push(v);path.reverse();return{safe:tg.get(u),nodes:path,dist:d};}
  for(let k=adjStart[u];k<adjStart[u+1];k++){if(closed.has(adjRoad[k]))continue;const v=adjTo[k],nd=d+adjLen[k];if(nd<dist[v]){dist[v]=nd;prev[v]=u;push(nd,v);}}}
 return null;}
function stepManual(m,dt,x,ctl){if(m.status!=='moving'&&m.status!=='idle')return;let move=dt*(m.speed||WALK),guard=0;m.status='idle';
 while(move>0&&guard++<400){
  if(m.to<0){const si=D.safe.findIndex((s,i)=>s.node===m.node&&safeOpen(i,x));if(si>=0){m.status='safe';m.stop=D.safe[si].name;return;}
   let nxt=-1;const d=ctl.dir();
   if(m.auto){const r=route(m.node,x,ctl.pref);if(r&&r.nodes.length>1)nxt=r.nodes[1];}
   else if(d){nxt=bestNeighbor(m.node,ctl.bearing(d),x);}
   if(nxt<0)break;ctl.clear();m.from=m.node;m.to=nxt;m.len=Math.max(1,dM(G.lon[m.from],G.lat[m.from],G.lon[nxt],G.lat[nxt]));m.prog=0;
   m.heading=Math.atan2((G.lon[nxt]-G.lon[m.from])*cosLat,G.lat[nxt]-G.lat[m.from])*180/Math.PI;}
  const rem=m.len-m.prog;m.status='moving';
  if(move>=rem){move-=rem;m.node=m.to;m.to=-1;m.prog=0;m.trail.push([G.lon[m.node],G.lat[m.node]]);}else{m.prog+=move;move=0;}}
 const f=m.to>=0?m.prog/m.len:0,a=m.to>=0?m.from:m.node,b=m.to>=0?m.to:m.node;
 m.lon=G.lon[a]+(G.lon[b]-G.lon[a])*f;m.lat=G.lat[a]+(G.lat[b]-G.lat[a])*f;
 for(const b of D.bombs){if(x>=b.strike&&dM(m.lon,m.lat,b.lon,b.lat)<=b.blast){m.status='caught';break;}}
 if(m.status==='idle'&&m.to<0)m.status='idle';}
function bestNeighbor(u,brg,x){const closed=closedAt(x);let best=-1,bd=100;
 for(let k=adjStart[u];k<adjStart[u+1];k++){if(closed.has(adjRoad[k]))continue;const v=adjTo[k];
  const h=Math.atan2((G.lon[v]-G.lon[u])*cosLat,G.lat[v]-G.lat[u])*180/Math.PI;let df=Math.abs(((h-brg+540)%360)-180);if(df<bd){bd=df;best=v;}}return best;}

/* ---------- plain-language directions for a route (node index list) ---------- */
function compass(b){return['north','north-east','east','south-east','south','south-west','west','north-west'][Math.round((((b%360)+360)%360)/45)%8];}
function turnWord(a,b){const d=((b-a+540)%360)-180;return Math.abs(d)<25?'':(d>0?'turn right':'turn left');}
function describeRoute(nodes){
 const steps=[];
 for(let i=0;i<nodes.length-1;i++){const u=nodes[i],v=nodes[i+1];
  const b=Math.atan2((G.lon[v]-G.lon[u])*cosLat,G.lat[v]-G.lat[u])*180/Math.PI,d=dM(G.lon[u],G.lat[u],G.lon[v],G.lat[v]),c=compass(b),last=steps[steps.length-1];
  if(last&&last.dir===c)last.m+=d;else steps.push({dir:c,m:d,b:b,turn:last?turnWord(last.b,b):''});}
 return steps.map(s=>({dir:s.dir,turn:s.turn,m:Math.max(10,Math.round(s.m/10)*10)}));}
return{dM,nearestNode,closedAt,safeOpen,route,bestNeighbor,stepManual,describeRoute,compass,WALK};
}
if(typeof module!=='undefined')module.exports={makeCore};
