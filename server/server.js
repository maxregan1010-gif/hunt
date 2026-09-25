const WebSocket = require('ws');
const PORT = process.env.PORT || 3000;

const wss = new WebSocket.Server({ port: PORT }, () => {
    console.log(`سرور بازی روی پورت ${PORT} اجرا شد`);
});

const players = {};

wss.on('connection', (ws) => {
    const id = Math.random().toString(36).substr(2, 9);
    players[id] = { x: 0, y: 2, z: 0 };

    ws.send(JSON.stringify({ type: 'init', id: id }));

    ws.on('message', (message) => {
        try {
            const data = JSON.parse(message);
            if (data.type === 'move') {
                players[id] = { x: data.x, y: data.y, z: data.z };
                
                // ارسال موقعیت جدید برای تمام بازیکنان آنلاین
                const broadcastData = JSON.stringify({ type: 'playersUpdate', players: players });
                wss.clients.forEach(client => {
                    if (client.readyState === WebSocket.OPEN) {
                        client.send(broadcastData);
                    }
                });
            }
        } catch (e) {
            console.error(e);
        }
    });

    ws.on('close', () => {
        delete players[id];
    });
});