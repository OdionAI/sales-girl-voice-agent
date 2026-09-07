import net from 'node:net';
import os from 'node:os';

// Bind only an explicitly supplied RFC1918 address already assigned to this Mac.
const host = process.argv[2];
const octets = (host ?? '').split('.').map(Number);
const privateIP = net.isIP(host ?? '') === 4 && (
  octets[0] === 10 || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
  (octets[0] === 192 && octets[1] === 168)
);
if (!privateIP || !Object.values(os.networkInterfaces()).flat().some(address => address?.address === host)) {
  throw new Error('Supply this Mac\'s private LAN IPv4 address. Wildcard/public binding is not allowed.');
}

const sockets = new Set();
const server = net.createServer(client => {
  const upstream = net.connect({ host: '127.0.0.1', port: 7880 });
  for (const socket of [client, upstream]) {
    sockets.add(socket);
    socket.setNoDelay(true);
    socket.on('error', () => { client.destroy(); upstream.destroy(); });
    socket.on('close', () => {
      sockets.delete(socket);
      (socket === client ? upstream : client).destroy();
    });
  }
  client.pipe(upstream).pipe(client);
});
server.on('error', error => { console.error(error.code); process.exitCode = 1; });
server.listen(7880, host, () => console.log(`Local signaling forwarder: ${host}:7880 -> 127.0.0.1:7880; PID ${process.pid}`));
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, () => {
    server.close();
    for (const socket of sockets) socket.destroy();
  });
}
