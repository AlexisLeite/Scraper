// One Chromium per group of workers. Each client owns its BrowserContexts;
// Playwright disposes those contexts when that client disconnects.
const fs = require('fs');
const readline = require('readline');
const { chromium } = require(process.argv[2]);
const output = process.argv[3];
const tokens = new Map();
const observed = new Map();
const targets = new Map();
const paused = new Set();
let commandId=0;
const attachedTargets=new Map();
function targetCommand(attached,method,params={}) {
  const id=++commandId;
  return new Promise((resolve,reject)=>{
    let receive;
    const finish=(error,result)=>{
      clearTimeout(timer);session.off('Target.receivedMessageFromTarget',receive);
      if(error)reject(error);else resolve(result);
    };
    const timer=setTimeout(()=>finish(new Error('Target command timed out')),1500);
    receive=event=>{
      if(event.sessionId!==attached.sessionId)return;
      const message=JSON.parse(event.message);
      if(message.id===id)finish(message.error?new Error(message.error.message):null,message.result);
    };
    session.on('Target.receivedMessageFromTarget',receive);
    session.send('Target.sendMessageToTarget',{sessionId:attached.sessionId,message:JSON.stringify({id,method,params})}).catch(finish);
  });
}
let lifecycleQueue=Promise.resolve();
function reconcile(activity){
  lifecycleQueue=lifecycleQueue.then(()=>lifecycle(activity,paused.has(activity)?'frozen':'active'));
  return lifecycleQueue;
}
async function lifecycle(activity,state) {
  for (const targetId of targets.get(activity)||[]) {
    try {
      let attached=attachedTargets.get(targetId);
      if(state==='frozen'){
        if(attached)continue;
        attached=await session.send('Target.attachToTarget',{targetId,flatten:false});
        attachedTargets.set(targetId,attached);
        await targetCommand(attached,'Debugger.enable');
        await targetCommand(attached,'Debugger.pause');
      } else if(attached){
        await targetCommand(attached,'Debugger.resume');
        await targetCommand(attached,'Debugger.disable');
        await session.send('Target.detachFromTarget',{sessionId:attached.sessionId});
        attachedTargets.delete(targetId);
      }
    } catch(error) {console.error(error.message);}
  }
}
let server, browser, session, stopping = false;
function publish(data) {
  const tmp = output + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data), {mode:0o600});
  fs.renameSync(tmp, output);
}
readline.createInterface({input:process.stdin}).on('line', line => {
  const value = JSON.parse(line);
  if (value.type === 'register') tokens.set(value.token, value.activity);
  if (value.type === 'release') {
    for (const [token,id] of tokens) if (id === value.activity) tokens.delete(token);
    for(const target of targets.get(value.activity)||[]){const attached=attachedTargets.get(target);if(attached)session.send('Target.detachFromTarget',{sessionId:attached.sessionId}).catch(()=>{});attachedTargets.delete(target);}
    observed.delete(value.activity);targets.delete(value.activity);paused.delete(value.activity);
  }
  if(value.type==='pause'){paused.add(value.activity);reconcile(value.activity);}
  if(value.type==='resume'){paused.delete(value.activity);reconcile(value.activity);}
});
async function close() {
  if (stopping) return;
  stopping = true;
  await server?.close().catch(()=>{});
  process.exit(0);
}
process.on('SIGTERM', close);
process.on('SIGINT', close);
process.stdin.on('end', close);
(async () => {
  server = await chromium.launchServer({headless:true,host:'127.0.0.1',
    ...(process.env.SCRAPER_DESKTOP_PROFILE==='1'?{channel:'chrome',args:['--disable-blink-features=AutomationControlled'],ignoreDefaultArgs:['--enable-automation']}:{}),
  });
  browser = await chromium.connect(server.wsEndpoint());
  session = await browser.newBrowserCDPSession();
  session.on('Tracing.dataCollected', ({value}) => {
    for (const event of value) {
      if (event.name !== 'TimeStamp') continue;
      const activity = tokens.get(event.args?.data?.message);
      if (!activity) continue;
      if (!observed.has(activity)) observed.set(activity,new Set());
      observed.get(activity).add(event.pid);
      if(!targets.has(activity))targets.set(activity,new Set());
      if(event.args?.data?.frame)targets.get(activity).add(event.args.data.frame);
    }
  });
  while (!stopping) {
    if(tokens.size===0){
      publish({endpoint:server.wsEndpoint(),pid:server.process().pid,activities:{},at:Date.now()});
      await new Promise(resolve=>setTimeout(resolve,1000));
      continue;
    }
    await session.send('Tracing.start', {traceConfig:{recordMode:'recordContinuously',includedCategories:['devtools.timeline'],traceBufferSizeInKb:1024},bufferUsageReportingInterval:1000,transferMode:'ReportEvents'});
    await new Promise(resolve=>setTimeout(resolve,1000));
    const complete = new Promise(resolve=>session.once('Tracing.tracingComplete',resolve));
    await session.send('Tracing.end');
    await complete;
    const {processInfo} = await session.send('SystemInfo.getProcessInfo');
    const renderers = new Set(processInfo.filter(p=>p.type==='renderer').map(p=>p.id));
    const owners = new Map();
    for (const [activity,pids] of observed) for (const pid of pids) {
      if (!renderers.has(pid)) {pids.delete(pid);continue;}
      if (!owners.has(pid)) owners.set(pid,[]);
      owners.get(pid).push(activity);
    }
    // Shared browser/GPU/network memory belongs to the global limit only.
    const activities = Object.fromEntries([...observed].map(([id,pids])=>[id,[...pids].filter(pid=>owners.get(pid)?.length===1)]));
    for(const id of paused)await reconcile(id);
    publish({endpoint:server.wsEndpoint(),pid:server.process().pid,activities,at:Date.now()});
  }
})().catch(async error => {
  if (!stopping) console.error(error.message);
  await close();
});
