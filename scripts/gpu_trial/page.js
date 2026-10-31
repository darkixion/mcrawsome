// Standalone trial. Each request decodes a new frame; RAW is not retained between requests.
const params = new URLSearchParams(location.search);
const browser = params.get('browser') || 'chromium';
const meta = await (await fetch('/meta')).json();
const canvas = document.querySelector('#gpu');
const jpegCanvas = document.querySelector('#jpeg');
canvas.width = jpegCanvas.width = meta.width;
canvas.height = jpegCanvas.height = meta.height;
const gl = canvas.getContext('webgl2', {antialias: false, preserveDrawingBuffer: true, failIfMajorPerformanceCaveat: true});
const result = {browser, userAgent: navigator.userAgent, meta, runs: {}, colour: []};
const status = document.querySelector('#status');
const now = () => performance.now();
const raf = () => new Promise(resolve => requestAnimationFrame(resolve));
function texture(unit, target) {
  const texture = gl.createTexture();
  gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(target, texture);
  for (const p of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(target, p, gl.NEAREST);
  for (const p of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(target, p, gl.CLAMP_TO_EDGE);
  return texture;
}
function summary(rows, elapsed) {
  const fields = Object.keys(rows[0]).filter(k => k !== 'index');
  const output = {fps: rows.length * 1000 / elapsed, frames: rows.length};
  for (const field of fields) {
    const values = rows.map(r => r[field]).filter(Number.isFinite).sort((a, b) => a - b);
    if (!values.length) continue;
    output[field] = {mean: values.reduce((a, b) => a + b, 0) / values.length, median: values[Math.floor(values.length / 2)], p99: values[Math.min(values.length - 1, Math.floor(values.length * .99))]};
  }
  return {...output, samples: rows};
}
let program;
let timer;
async function setup() {
  if (!gl) throw new Error('Hardware WebGL2 unavailable');
  const debug = gl.getExtension('WEBGL_debug_renderer_info');
  result.renderer = gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER);
  result.glVersion=gl.getParameter(gl.VERSION);result.glVendor=gl.getParameter(gl.VENDOR);
  if (/swiftshader|llvmpipe|softpipe|software/i.test(result.renderer)) throw new Error('Software renderer is not a GPU result');
  timer=gl.getExtension('EXT_disjoint_timer_query_webgl2');
  result.timerExtension=!!timer;
  program = gl.createProgram();
  for (const [type, source] of [[gl.VERTEX_SHADER, '#version 300 es\nin vec2 corner;void main(){gl_Position=vec4(corner,0,1);}'], [gl.FRAGMENT_SHADER, await (await fetch('/develop.frag')).text()]]) {
    const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
    gl.attachShader(program, shader);
  }
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  gl.useProgram(program);
  const vertex = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, vertex);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
  const corner = gl.getAttribLocation(program, 'corner');gl.enableVertexAttribArray(corner);gl.vertexAttribPointer(corner,2,gl.FLOAT,false,0,0);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);
  texture(1,gl.TEXTURE_2D);
  const calibration = new Float32Array(await (await fetch('/calibration')).arrayBuffer());
  gl.texImage2D(gl.TEXTURE_2D,0,gl.RG32F,meta.raw_width,meta.raw_height,0,gl.RG,gl.FLOAT,calibration);
  texture(2,gl.TEXTURE_3D);
  const table = new Float32Array(await (await fetch('/table')).arrayBuffer());
  gl.texImage3D(gl.TEXTURE_3D,0,gl.RGB32F,meta.size,meta.size,meta.size,0,gl.RGB,gl.FLOAT,table);
  texture(0,gl.TEXTURE_2D);
  gl.texImage2D(gl.TEXTURE_2D,0,gl.R16UI,meta.raw_width,meta.raw_height,0,gl.RED_INTEGER,gl.UNSIGNED_SHORT,null);
  const loc = name => gl.getUniformLocation(program,name);
  gl.uniform1f(loc('exposure'),meta.exposure);
  gl.uniform1i(loc('rawFrame'),0);gl.uniform1i(loc('calibration'),1);gl.uniform1i(loc('lut'),2);
  gl.uniform2i(loc('rawSize'),meta.raw_width,meta.raw_height);gl.uniform2i(loc('outputSize'),meta.width,meta.height);
  gl.uniform4iv(loc('cfa'),meta.cfa);gl.uniform1i(loc('size'),meta.size);gl.uniform1i(loc('rotate'),Number(meta.rotate));
  const m=meta.matrix;gl.uniformMatrix3fv(loc('matrix'),false,new Float32Array([m[0],m[3],m[6],m[1],m[4],m[7],m[2],m[5],m[8]]));
  gl.viewport(0,0,meta.width,meta.height);
  if(gl.getError()) throw new Error('WebGL setup error');
}
const socket=new WebSocket(`ws://127.0.0.1:${meta.ws_port}`);
socket.binaryType='arraybuffer';
const connected=new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=()=>reject(new Error('WebSocket failed'));});
let pending;
socket.onmessage=event=>{
  if(!pending) return;
  if(typeof event.data==='string') pending.header=JSON.parse(event.data);
  else {const active=pending;pending=null;active.resolve({data:event.data,server:active.header.timings});}
};
socket.onclose=()=>{if(pending){pending.reject(new Error('WebSocket closed'));pending=null;}};
async function frame(route,method,index) {
  await connected;
  const started=now();
  const {data,server}=await new Promise((resolve,reject)=>{
    pending={resolve,reject};socket.send(JSON.stringify({route,method,index}));
  });
  const received=now();
  const timings={index,...server,fetch_ms:received-started,transfer_overhead_ms:Math.max(0,received-started-server.server_ms)};
  if(route==='raw') {
    const uploadQuery=timer ? gl.createQuery() : null;
    const drawQuery=timer ? gl.createQuery() : null;
    const t=now();gl.activeTexture(gl.TEXTURE0);
    if(timer) gl.beginQuery(timer.TIME_ELAPSED_EXT,uploadQuery);
    gl.texSubImage2D(gl.TEXTURE_2D,0,0,0,meta.raw_width,meta.raw_height,gl.RED_INTEGER,gl.UNSIGNED_SHORT,new Uint16Array(data));
    if(timer) gl.endQuery(timer.TIME_ELAPSED_EXT);
    const uploaded=now();
    gl.uniform1i(gl.getUniformLocation(program,'quad'),Number(method==='quad'));
    if(timer) gl.beginQuery(timer.TIME_ELAPSED_EXT,drawQuery);
    gl.drawArrays(gl.TRIANGLES,0,3);
    if(timer) gl.endQuery(timer.TIME_ELAPSED_EXT);
    const submitted=now();
    // WebGL commands cross a browser process boundary. Fence completion explicitly.
    const fence=gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE,0);gl.flush();
    let state=gl.clientWaitSync(fence,0,0);
    while(state===gl.TIMEOUT_EXPIRED) {
      if(now()-submitted>5000) throw new Error('GPU fence timed out');
      await new Promise(resolve=>setTimeout(resolve,0));
      state=gl.clientWaitSync(fence,0,0);
    }
    if(state===gl.WAIT_FAILED) throw new Error('GPU fence failed');
    gl.deleteSync(fence);
    timings.upload_submit_ms=uploaded-t;timings.draw_submit_ms=submitted-uploaded;
    timings.gpu_completion_wait_ms=now()-submitted;
    if(timer) {
      while(!gl.getQueryParameter(drawQuery,gl.QUERY_RESULT_AVAILABLE)) await raf();
      if(gl.getParameter(timer.GPU_DISJOINT_EXT)) throw new Error('GPU timer disjoint');
      timings.upload_gpu_ms=gl.getQueryParameter(uploadQuery,gl.QUERY_RESULT)/1e6;
      timings.develop_gpu_ms=gl.getQueryParameter(drawQuery,gl.QUERY_RESULT)/1e6;
      gl.deleteQuery(uploadQuery);gl.deleteQuery(drawQuery);
    }
    if(gl.getError()) throw new Error('WebGL draw error');
  } else {
    const t=now();const image=await createImageBitmap(new Blob([data],{type:'image/jpeg'}));
    timings.jpeg_decode_ms=now()-t;
    const draw=now();jpegCanvas.getContext('2d').drawImage(image,0,0);image.close();timings.draw_ms=now()-draw;
  }
  // Wait for the next animation frame: includes the page's display cadence, not a compositor proof.
  await raf();timings.total_ms=now()-started;
  return timings;
}
async function run() {
  try {
    await setup();
    for(const [route,method] of [['cpu','quad'],['compute','quad'],['compute','bilinear'],['raw','quad'],['raw','bilinear']]) {
      canvas.hidden=route!=='raw';jpegCanvas.hidden=route==='raw';
      await frame(route,method,0);
      for(const mode of ['playback','scrub']) {
        const key=`${route}_${method}_${mode}`;status.textContent=`${browser}: ${key}`;
        // Local harness uses this marker to sample process CPU and RSS for each timed run.
        await fetch(`/phase?name=${key}`).catch(()=>{});
        const order=Array.from({length:meta.count},(_,i)=>i);
        if(mode==='scrub') order.push(...Array.from({length:meta.count-2},(_,i)=>meta.count-2-i));
        const rows=[];const start=now();
        for(let i=0;i<meta.frames;i++) rows.push(await frame(route,method,order[i%order.length]));
        result.runs[key]=summary(rows,now()-start);
      }
    }
    await fetch('/phase?name=parity');
    jpegCanvas.hidden=true;canvas.hidden=false;
    for(const index of [0,Math.min(30,meta.count-1),Math.min(59,meta.count-1)]) {
      const reference=new Uint8Array(await (await fetch(`/reference?index=${index}`)).arrayBuffer());
      for(const method of ['quad','bilinear']) {
        await frame('raw',method,index);
        const pixels=new Uint8Array(meta.width*meta.height*4);
        const t=now();gl.readPixels(0,0,meta.width,meta.height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
        const readback=now()-t;
        const errors=new Uint8Array(reference.length);
        for(let y=0;y<meta.height;y++) for(let x=0;x<meta.width;x++) for(let c=0;c<3;c++) {
          const i=(y*meta.width+x)*3+c;errors[i]=Math.abs(reference[i]-pixels[((meta.height-1-y)*meta.width+x)*4+c]);
        }
        const mean=errors.reduce((a,b)=>a+b,0)/errors.length;errors.sort();
        result.colour.push({index,method,mean_8bit:mean,p99_8bit:errors[Math.floor(errors.length*.99)],readback_ms:readback});
      }
    }
  } catch(error) { result.error=String(error.stack||error); }
  socket.close();
  result.jsHeapBytes=performance.memory?.usedJSHeapSize ?? null;
  await fetch('/result',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(result)});
  status.textContent=result.error ? 'Failed: '+result.error : 'Complete';
  document.querySelector('#result').textContent=JSON.stringify({...result,runs:Object.fromEntries(Object.entries(result.runs).map(([k,v])=>[k,{...v,samples:undefined}]))},null,2);
  window.trialResult=result;window.trialDone=true;
}
run();
