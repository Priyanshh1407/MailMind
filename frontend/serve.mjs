// Built UI server with loopback default and owned stdin shutdown.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root=path.join(path.dirname(fileURLToPath(import.meta.url)),'dist');
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.json':'application/json'};
const server=http.createServer((request,response)=>{
  if(!['GET','HEAD'].includes(request.method)){response.writeHead(405);response.end();return;}
  let pathname;
  try{pathname=decodeURIComponent(new URL(request.url,'http://localhost').pathname);}catch{response.writeHead(400);response.end();return;}
  // Supervisor status (service restarts, crash-loop warning) for the dashboard,
  // available even while the API is down. Contains no secrets.
  if(pathname==='/mailmind-supervisor.json'){
    const statusPath=process.env.MAILMIND_SUPERVISOR_STATUS;
    fs.readFile(statusPath||'',(error,buffer)=>{
      if(error||!statusPath){response.writeHead(404,{'Cache-Control':'no-store'});response.end();return;}
      response.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});
      response.end(request.method==='HEAD'?undefined:buffer);
    });
    return;
  }
  let target=path.resolve(root,'.'+pathname);
  if(target!==root&&!target.startsWith(root+path.sep)){response.writeHead(403);response.end();return;}
  if(!fs.existsSync(target)||!fs.statSync(target).isFile())target=path.join(root,'index.html');
  fs.readFile(target,(error,buffer)=>{
    if(error){response.writeHead(503);response.end('Build the frontend first');return;}
    response.writeHead(200,{'Content-Type':types[path.extname(target)]||'application/octet-stream','X-Content-Type-Options':'nosniff','Cache-Control':'no-cache'});response.end(request.method==='HEAD'?undefined:buffer);
  });
});
function stop(){server.close(()=>process.exit(0));setTimeout(()=>process.exit(1),3000).unref();}
process.on('SIGTERM',stop);process.on('SIGINT',stop);
process.stdin.setEncoding('utf8');process.stdin.on('data',data=>{if(data.includes('STOP'))stop();});
if(process.env.MAILMIND_STDIN_CONTROL!=='false')process.stdin.on('end',stop);
server.listen(5173,process.env.MAILMIND_UI_HOST||'127.0.0.1',()=>console.log('MailMind built UI ready on port 5173'));
