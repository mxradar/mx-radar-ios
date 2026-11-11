/* Marker preferences live with the existing dock so updates need no relay restart. */
(() => {
  const body = document.querySelector('#dock > .pan-body');
  if (!body || typeof PREF === 'undefined') return;
  // Also support the HTML already held by the running parser, whose original
  // range starts at 80. Its inline initializer clamps saved values before
  // this deferred script runs, so retain the extended preference separately.
  const fontSlider = document.getElementById('s-fontsize');
  if (fontSlider) {
    let savedFont = null;
    try { savedFont = localStorage.getItem('nr2_fontsize_extended'); } catch {}
    const fontValue = Number(savedFont === null ? PREF.fontsize : savedFont);
    fontSlider.min = '40';
    fontSlider.value = String(Math.max(40, Math.min(220, Number.isFinite(fontValue) ? fontValue : 100)));
    fontSlider.addEventListener('input', () => {
      try { localStorage.setItem('nr2_fontsize_extended', fontSlider.value); } catch {}
    });
    fontSlider.dispatchEvent(new Event('input', {bubbles:true}));
  }
  const defaults = {opname:1, teamnum:1, namegap:34, geargap:8, distgap:10};
  for (const [key, fallback] of Object.entries(defaults)) {
    PREF_DEF[key] = fallback;
    const saved = localStorage.getItem('nr2_' + key);
    const value = saved === null ? fallback : Number(saved);
    PREF[key] = Number.isFinite(value) ? value : fallback;
  }
  function toggle(id, title, hint, after) {
    const row = document.createElement('div'); row.className = 'row';
    row.innerHTML = '<div class="row-col"><div class="row-t">' + title
      + '</div><div class="row-s">' + hint + '</div></div><button class="sw" id="s-' + id + '" type="button"></button>';
    body.append(row);
    bindToggle(['s-' + id], id, after);
    const el = row.querySelector('button');
    const sync = () => el.setAttribute('aria-pressed', String(!!PREF[id]));
    el.addEventListener('click', sync); sync();
  }
  function redraw() {
    if (lastSnap) draw(lastSnap);
    if (R3) { R3.setPrefs(PREF); if (lastSnap && is3d()) R3.update(loot3dSnapshot(lastSnap)); }
  }
  // Keep the existing rendering and caches; only change the independent fields.
  tagHtml = function(e, d) {
    const parts = [], col = entColor(e);
    const st = e.status_key ? STATE_TXT[e.status_key] : null;
    if (st) parts.push('<b>' + st + '</b>');
    if (e._out_of_range) parts.push('<span class="far">超距·最后位置</span>');
    if (e.spawn_mark) parts.push('<span class="sp">出生点'
      + (e.age_sec != null ? ' ' + agoTxt(e.age_sec) : '') + '</span>');
    if (PREF.opname && e.hero) parts.push('<span class="hero">[' + esc(cleanUeName(e.hero)) + ']</span>');
    const n = e.name ? (e.is_bot && !e.hero ? e.name + '（人机）' : e.name) : '';
    if (PREF.name) parts.push('<span class="player-name">' + (esc(cleanUeName(n)) || '未知目标') + '</span>');
    const w = cleanWeapon(e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon)) || '';
    if (PREF.wpn && w) parts.push('<span class="wpn">[' + esc(w) + ']</span>');
    return parts.length ? '<div class="tb-wrap"><div class="tag' + (e._out_of_range ? ' out-range' : '')
      + '" style="--tc:' + col + '">' + parts.join('') + '</div></div>' : '';
  };
  const icon = mkIcon;
  mkIcon = (e, col, rot) => icon(PREF.teamnum ? e : {...e, team:0}, col, rot);
  const radius = () => 6.5 * (Number(PREF.dotsize) || 100) / 100;
  const nameAnchor = () => Math.round(radius() + Math.round(24 * (Number(PREF.fontsize) || 100) / 100) + Number(PREF.namegap));
  const gear = updGear, distance = updDist, foe = updFoe;
  updGear = (key, ll, html) => gear(key, ll, html, Math.round(radius() + 13 + Number(PREF.geargap)));
  updDist = (key, ll, html) => distance(key, ll, html, -Math.round(radius() + Number(PREF.distgap)));
  updFoe = (key, ll, on, ay, down) => foe(key, ll, on, nameAnchor(), down);
  const render = draw;
  draw = function(state) {
    const result = render(state);
    const entities = new Map((state.entities || []).map(e => [e.key, e]));
    for (const [key, marker] of tags) {
      if (!['player','ai','unknown'].includes(entities.get(key)?.kind)) continue;
      const options = marker.options.icon.options, y = nameAnchor();
      if (options.iconAnchor[1] !== y) marker.setIcon(L.divIcon({...options, iconAnchor:[options.iconAnchor[0], y]}));
    }
    return result;
  };
  toggle('opname', '干员职业', '独立显示银翼等干员职业', () => _rc.tag.clear());
  toggle('teamnum', '头像队伍号', '显示头像圆心上的小队编号', () => _rc.icon.clear());
  // Older pages registered the distance button twice, cancelling each click.
  const oldDistance = document.getElementById('s-dist');
  if (oldDistance) {
    const clean = oldDistance.cloneNode(true); oldDistance.replaceWith(clean);
    bindToggle(['s-dist'], 'dist', () => {
      if (!PREF.dist) { for (const marker of distbars.values()) marker.remove(); distbars.clear(); }
    });
  }
  for (const [key, title, max] of [['namegap','玩家名距圆心',140], ['geargap','盔甲距圆心',100], ['distgap','距离行距圆心',100]]) {
    const row = document.createElement('div'); row.className = 'sld';
    row.innerHTML = '<span>' + title + '</span><input type="range" id="s-' + key
      + '" min="0" max="' + max + '" step="1"><span class="v tnum" id="v-' + key + '"></span>';
    body.append(row);
    PREF[key] = Math.max(0, Math.min(max, Number(PREF[key])));
    bindSlider('s-' + key, key, v => v + 'px', redraw);
  }
  redraw();
})();

/* Keep the existing controls and listeners; only reorganize the settings panel. */
(() => {
  const dock = document.getElementById('dock');
  if (!dock) return;
  const body = dock.querySelector(':scope > .pan-body');
  const pages = [
    {key:'display',label:'显示',title:'人物与标记',hint:'选择地图上需要的信息',groups:[
      ['地图',['s-map']],
      ['侧栏外观',['panelthemeseg']],
      ['人物显示',['s-mate','s-ai','s-opname','s-name','s-teamnum','s-wpn','s-gear','s-dist','s-hp','s-foe','s-cone','s-trail','s-roster'],true],
      ['标记样式',['s-dotsize','s-fontsize','s-tagw','s-tagop','s-gunlen']],
      ['\u6807\u8bb0\u884c\u8ddd',['s-namegap','s-geargap','s-distgap']]]},
    {key:'scene',label:'3D 场景',title:'3D 场景',hint:'拖动即生效 · 自动保存',groups:[
      ['建筑材质',['s-walltrans','s-floortrans']],
      ['人物模型与朝向',['model3dseg','direction3dseg','directionanchor3dseg','s-charsize']],
      ['人物颜色',['s-visiblecolor3d','s-occludedcolor3d']],
      ['第一视角',['s-fov','s-fpvheight','s-fpvtau']],
      ['渲染性能',['q3dseg','fpscapseg']]]},
    {key:'loot',label:'物资',title:'物资与容器',hint:'按需显示，减少地图遮挡',groups:[
      ['显示类型',['s-loot','s-box','s-container','s-aibox'],true],
      ['物资品质',['pick-loot']]]},
    {key:'alerts',label:'预警',title:'距离与预警',hint:'视图顶部可开关屏外预警',groups:[
      ['接近提醒',['s-alert']],
      ['屏外预警',['s-warnd','s-warnr','s-warnsz']]]},
  ];
  const shell = document.createElement('div'); shell.className='dock-layout';
  const nav = document.createElement('div');nav.className='dock-tabs';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','设置分类');nav.setAttribute('aria-orientation','vertical');
  const content=document.createElement('div');content.className='dock-content';
  const tabs=[],panels=[];
  for (const page of pages) {
    const tab=document.createElement('button');tab.type='button';tab.id='dock-tab-'+page.key;tab.textContent=page.label;
    tab.setAttribute('role','tab');tab.setAttribute('aria-controls','dock-page-'+page.key);tab.dataset.page=page.key;nav.append(tab);tabs.push(tab);
    const panel=document.createElement('section');panel.id='dock-page-'+page.key;panel.className='dock-page';panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tab.id);
    const head=document.createElement('header');head.className='dock-page-head';
    const title=document.createElement('h2');title.textContent=page.title;const hint=document.createElement('p');hint.textContent=page.hint;head.append(title,hint);panel.append(head);
    for(const [name,ids,grid] of page.groups){
      const section=document.createElement('section');section.className='dock-group';const label=document.createElement('h3');label.textContent=name;section.append(label);
      const controls=document.createElement('div');controls.className=grid?'dock-switches':'dock-controls';
      for(const id of ids){
        const el=document.getElementById(id);if(!el)continue;
        const row=el.closest('.row,.sld,.pick') || el;
        if(el.classList.contains('sw')){const caption=row.querySelector('.row-t')?.textContent || id;el.setAttribute('aria-label',caption);row.title=row.querySelector('.row-s')?.textContent || caption;}
        if(el.matches('input[type="range"]')) el.setAttribute('aria-label',row.querySelector('span')?.textContent || id);
        controls.append(row);
      }
      section.append(controls);panel.append(section);
    }
    if(page.key==='scene'){
      const note=document.createElement('p');note.className='dock-note';note.textContent='透明度 0% 为实心，100% 为全透明。仅改变模型外观。';panel.append(note);
    }
    content.append(panel);panels.push(panel);
  }
  // Unexpected future controls stay reachable rather than disappearing silently.
  const remaining=[...body.children].filter(el=>el.querySelector('button,input,select')||el.matches('button,input,select'));
  if(remaining.length){const group=document.createElement('section');group.className='dock-group';group.append(...remaining);panels[0].append(group);}
  shell.append(nav,content);body.replaceWith(shell);
  const diagnostics=document.getElementById('gateway-diagnostics');if(diagnostics)dock.append(diagnostics);
  function select(key,focus=false){
    if(!pages.some(p=>p.key===key))key='display';
    tabs.forEach(tab=>{const active=tab.dataset.page===key;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;if(active&&focus)tab.focus();});
    panels.forEach((panel,i)=>panel.hidden=pages[i].key!==key);
    try{localStorage.setItem('gateway_dock_tab',key);}catch{}
  }
  tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>select(tab.dataset.page));tab.addEventListener('keydown',e=>{
    let next;if(e.key==='ArrowDown'||e.key==='ArrowRight')next=(i+1)%tabs.length;
    if(e.key==='ArrowUp'||e.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;
    if(e.key==='Home')next=0;if(e.key==='End')next=tabs.length-1;
    if(next!==undefined){e.preventDefault();select(tabs[next].dataset.page,true);}
  });});
  let saved='display';try{saved=localStorage.getItem('gateway_dock_tab')||saved;}catch{}
  select(saved);document.getElementById('tools-sub').textContent='XX';
})();
