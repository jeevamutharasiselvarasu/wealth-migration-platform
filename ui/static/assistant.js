/* Meeting assistant, from the Xodus suite template (mock meetings; no calendar integration in this build). */
(function () {
var ROLE = (document.documentElement.getAttribute("data-trole") || "IL");
var SPACES = {deal: {name: "Deal Room"}, dm: {name: "Data Migration"}, gov: {name: "Governance"}};
var ACCESS = {deal: ["EVP", "DT", "IL", "CR"], dm: ["MA", "VS", "IL", "PA", "CR"], gov: ["EVP", "IL", "CR", "PA"]};
function can(sp) { return ACCESS[sp].indexOf(ROLE) > -1; }
function esc(x) { return String(x).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
var MEET=[
 {id:'m1',sp:'deal',t:'CoreCap leadership call',w:'Mon 26 Oct',who:'Jordan Hale, Sam Rivera, Avery Chen',dec:['Keep Orion as the target platform.','Advisor retention is the main risk to watch.'],act:['Sam to request the household count by advisor.','Avery to draft the integration estimate.'],keys:['retention','advisor','orion','target','estimate','leadership']},
 {id:'m2',sp:'deal',t:'Diligence data review',w:'Thu 29 Oct',who:'Sam Rivera, Avery Chen, Priya Nair',dec:['Cost basis completeness varies by custodian.','Treat advisor-as-portfolio-manager accounts as a separate scope item.'],act:['Priya to profile cost basis gaps once files land.'],keys:['cost basis','diligence','custodian','portfolio manager','scope']},
 {id:'m3',sp:'dm',t:'Migration stand-up',w:'Fri 30 Oct',who:'Priya Nair, Marcus Webb, Vivek Sarathy',dec:['Approve the undefined columns in the returns file.','Hold promotion until unmapped security identifiers are resolved.'],act:['Priya to review the five proposed business rules.','Marcus to prepare reconciliation tolerances.'],keys:['stand-up','identifier','cusip','unmapped','rules','coverage','blocker','returns','tolerance']},
 {id:'m4',sp:'gov',t:'Gate review',w:'Fri 30 Oct',who:'Avery Chen, Dana Ortiz, Vivek Sarathy',dec:['Keep every stage gated for this migration.','Dual sign-off stays on for the target load.'],act:['Dana to confirm the audit retention setting.'],keys:['gate','policy','sign-off','audit','retention','governance']}];
var INS=[
 {sp:'deal',t:'Advisor retention came up in both deal calls',d:'It is the most repeated risk. Consider adding it to the decision memo.'},
 {sp:'dm',t:'The unmapped identifier blocker was raised in three stand-ups',d:'It has been open longer than any other item and halts promotion.'},
 {sp:'gov',t:'Two actions are older than a week',d:'Both belong to the audit retention review.'},
 {sp:'dm',t:'Reconciliation tolerances have no owner yet',d:'Marcus has the action but nothing is scheduled.'}];
var CHIPS=['What did we decide about the target platform?','What actions are open?','Why is the mapping blocked?','What did we decide on gates?'];
var MT={tab:'ask',thread:[],conn:{teams:true,zoom:false}};
function vis(x){return can(x.sp)}
function answer(q){
 var s=q.toLowerCase(),best=null,sc=0;
 MEET.filter(vis).forEach(function(m){var n=0;m.keys.forEach(function(k){if(s.indexOf(k)>-1)n++});if(s.indexOf('action')>-1||s.indexOf('open')>-1)n+=m.act.length?1:0;if(n>sc){sc=n;best=m}});
 var hidden=MEET.filter(function(m){return !vis(m)&&m.keys.some(function(k){return s.indexOf(k)>-1})});
 if(!best){return hidden.length?{a:'That topic is discussed in a space your role cannot see ('+hidden.map(function(m){return SPACES[m.sp].name}).filter(function(v,i,a){return a.indexOf(v)==i}).join(', ')+'), so I cannot answer it here.',src:[]}:{a:'I could not find that in the meetings you can see. Try asking about a decision, an action or a blocker.',src:[]}}
 var act=(s.indexOf('action')>-1||s.indexOf('open')>-1);
 return{a:act?'Open actions from '+best.t+': '+best.act.join(' '):'From '+best.t+' ('+best.w+'): '+best.dec.join(' '),src:[best.t,SPACES[best.sp].name]}}
function mdrOpen(){var d=document.getElementById('mdr');if(d){d.classList.add('open');mdrRender()}}
function mdrClose(){var d=document.getElementById('mdr');if(d)d.classList.remove('open')}
function mdrRender(){
 var b=document.getElementById('mdrBody'),f=document.getElementById('mdrFoot'),h='';
 document.querySelectorAll('#mdr .tab').forEach(function(t){t.classList.toggle('on',t.dataset.t==MT.tab)});
 if(MT.tab=='ask'){
  h=MT.thread.length?MT.thread.map(function(m){return m.u?'<div class="msg u">'+esc(m.u)+'</div>':'<div class="msg a">'+esc(m.a)+(m.src.length?'<div class="src">'+m.src.map(function(x){return '<span>'+esc(x)+'</span>'}).join('')+'</div>':'')+'</div>'}).join(''):'<div class="empty">Ask about decisions, actions and blockers from your meetings. You only get answers from spaces your role can see.</div>';
  f.hidden=false;f.innerHTML='<div class="chips">'+CHIPS.map(function(c){return '<button class="qchip" type="button" data-q="'+esc(c)+'">'+esc(c)+'</button>'}).join('')+'</div><form class="row" id="mdrForm"><input id="mdrIn" type="text" placeholder="Ask about a meeting" aria-label="Ask about a meeting"><button class="btn p" type="submit">Ask</button></form>'}
 else if(MT.tab=='notes'){
  f.hidden=true;var ms=MEET.filter(vis);h=ms.length?ms.map(function(m){return '<div class="mtg"><strong>'+esc(m.t)+'</strong><small>'+m.w+' · '+SPACES[m.sp].name+' · '+esc(m.who)+'</small><div><b>Decisions</b><ul style="margin:4px 0 0;padding-left:18px">'+m.dec.map(function(x){return '<li>'+esc(x)+'</li>'}).join('')+'</ul></div><div><b>Actions</b><ul style="margin:4px 0 0;padding-left:18px">'+m.act.map(function(x){return '<li>'+esc(x)+'</li>'}).join('')+'</ul></div></div>'}).join(''):'<div class="empty">No meetings are visible to your role.</div>'}
 else if(MT.tab=='insights'){
  f.hidden=true;var is=INS.filter(vis);h=is.length?is.map(function(i){return '<div class="ins"><b>'+esc(i.t)+'</b>'+esc(i.d)+'<div style="margin-top:6px"><span class="chip '+({deal:'c',dm:'b',gov:'g'}[i.sp])+'">'+SPACES[i.sp].name+'</span></div></div>'}).join(''):'<div class="empty">No insights are visible to your role.</div>'}
 else{
  f.hidden=true;h='<div class="lockn">In the real build the assistant joins through the calendar invite. Here you can see how that is switched on.</div>'
  +[['teams','Microsoft Teams'],['zoom','Zoom']].map(function(c){return '<div class="conn"><span><b>'+c[1]+'</b><br><small style="color:var(--muted)">'+(MT.conn[c[0]]?'Joins invited meetings and writes notes':'Not connected')+'</small></span><button class="btn g sm" type="button" data-c="'+c[0]+'">'+(MT.conn[c[0]]?'Disconnect':'Connect')+'</button></div>'}).join('')
  +'<div class="conn"><span><b>Next meeting</b><br><small style="color:var(--muted)">Migration stand-up · Mon 2 Nov</small></span><span class="pill '+(MT.conn.teams?'approved':'queued')+'">'+(MT.conn.teams?'Will join':'Will not join')+'</span></div>'}
 b.innerHTML=h;b.scrollTop=b.scrollHeight}
function meeting(){
 var d=document.createElement('aside');d.id='mdr';d.className='mdr';d.setAttribute('aria-label','Meeting assistant');
 d.innerHTML='<div class="mdr-h"><div><strong>Meeting assistant</strong><small>Notes, questions and insights across your spaces</small></div><button class="tbtn" id="mdrX" type="button" aria-label="Close">Close</button></div><div class="tabs"><button class="tab on" data-t="ask" type="button">Ask</button><button class="tab" data-t="notes" type="button">Notes</button><button class="tab" data-t="insights" type="button">Insights</button><button class="tab" data-t="connect" type="button">Meetings</button></div><div class="mdr-b" id="mdrBody"></div><div class="mdr-f" id="mdrFoot"></div>';
 document.body.appendChild(d);
 var f=document.createElement('button');f.className='mfab';f.type='button';f.innerHTML='<i></i>Meeting assistant';f.addEventListener('click',mdrOpen);document.body.appendChild(f);
 d.addEventListener('click',function(e){
  var t=e.target.closest('.tab');if(t){MT.tab=t.dataset.t;mdrRender();return}
  var q=e.target.closest('[data-q]');if(q){ask(q.dataset.q);return}
  var c=e.target.closest('[data-c]');if(c){MT.conn[c.dataset.c]=!MT.conn[c.dataset.c];mdrRender();return}
  if(e.target.id=='mdrX')mdrClose()});
 d.addEventListener('submit',function(e){e.preventDefault();var i=document.getElementById('mdrIn');if(i&&i.value.trim()){ask(i.value.trim())}});
 document.addEventListener('keydown',function(e){if(e.key=='Escape')mdrClose()})}
function ask(q){MT.thread.push({u:q});var a=answer(q);MT.thread.push({a:a.a,src:a.src});MT.tab='ask';mdrRender()}
function openAssistant(tab){MT.tab=tab||'ask';mdrOpen()}

window.openAssistant = openAssistant;
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", meeting); else meeting();
})();
