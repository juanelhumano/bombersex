import asyncio
import json
import uuid
import random
import os
from aiohttp import web, WSMsgType

# Constantes base
WALL_HARD = 1
WALL_SOFT = 2
FLOOR = 0
ITEM_FIRE = 3
ITEM_SPEED = 4
ITEM_GHOST = 5
ITEM_KICK = 6 
ITEM_AMMO = 7

class BombermanServer:
    def __init__(self):
        self.clients = {} # ws -> pid
        self.players = {} # pid -> data
        self.game_started = False
        self.grid_size = 15
        self.map = []
        self.bombs = [] 
        self.regenerate_map(15)
        
        asyncio.create_task(self.physics_loop())

    def regenerate_map(self, size):
        self.grid_size = size
        self.map = [[0 for _ in range(size)] for _ in range(size)]
        for y in range(size):
            for x in range(size):
                if x == 0 or x == size-1 or y == 0 or y == size-1:
                    self.map[y][x] = WALL_HARD
                elif x % 2 == 0 and y % 2 == 0:
                    self.map[y][x] = WALL_HARD
                elif random.random() < 0.45 and not ((x < 3 and y < 3) or (x > size-4 and y > size-4)): 
                    self.map[y][x] = WALL_SOFT
    
    def get_start_pos(self, index):
        s = self.grid_size
        c1 = (64, 64)
        c2 = (s*64 - 128, 64)
        c3 = (64, s*64 - 128)
        c4 = (s*64 - 128, s*64 - 128)
        corners = [c1, c2, c3, c4]
        return corners[index % 4]

    async def broadcast(self, message, exclude=None):
        if not self.clients: return
        data = json.dumps(message)
        disconnected = []
        for ws in list(self.clients.keys()):
            if ws != exclude:
                try: 
                    # aiohttp usa send_str
                    await ws.send_str(data)
                except: 
                    disconnected.append(ws)
        
        for ws in disconnected: 
            await self.handle_disconnect(ws)

    async def handle_disconnect(self, ws):
        if ws in self.clients:
            pid = self.clients[ws]
            del self.clients[ws]
            if pid in self.players:
                del self.players[pid]
            
            new_host = list(self.players.keys())[0] if self.players else None
            
            await self.broadcast({
                'type': 'player_left', 
                'id': pid,
                'new_host': new_host
            })
            await self.check_win_condition()

    async def physics_loop(self):
        while True:
            await asyncio.sleep(0.05)
            if not self.bombs: continue
            
            moved = False
            for b in self.bombs:
                if b.get('vx', 0) != 0 or b.get('vy', 0) != 0:
                    new_x = b['x'] + b['vx'] * 16
                    new_y = b['y'] + b['vy'] * 16
                    
                    gx = int((new_x + 32) // 64)
                    gy = int((new_y + 32) // 64)
                    
                    if 0 <= gx < self.grid_size and 0 <= gy < self.grid_size:
                        if self.map[gy][gx] != FLOOR:
                            b['vx'] = 0; b['vy'] = 0
                            b['x'] = round(b['x'] / 64) * 64
                            b['y'] = round(b['y'] / 64) * 64
                            moved = True
                        else:
                            b['x'] = new_x
                            b['y'] = new_y
                            moved = True
                    else:
                        b['vx'] = 0; b['vy'] = 0
            
            if moved:
                await self.broadcast({'type': 'bombs_update', 'bombs': self.bombs})

    async def bomb_logic(self, bomb_obj):
        await asyncio.sleep(3.0)
        if bomb_obj not in self.bombs: return
        self.bombs.remove(bomb_obj)
        await self.broadcast({'type': 'bombs_update', 'bombs': self.bombs})
        
        bx, by = bomb_obj['x'], bomb_obj['y']
        brange = bomb_obj['range']
        gx, gy = int((bx+32) // 64), int((by+32) // 64)
        explosion_cells = []
        directions = [(0,0), (0,-1), (0,1), (-1,0), (1,0)]

        for dx, dy in directions:
            for i in range(brange if (dx, dy) != (0,0) else 1):
                dist = i + 1 if (dx, dy) != (0,0) else 0
                tx, ty = gx + (dx * dist), gy + (dy * dist)
                if 0 <= tx < self.grid_size and 0 <= ty < self.grid_size:
                    cell = self.map[ty][tx]
                    if cell == WALL_HARD: break
                    explosion_cells.append({'x': tx * 64, 'y': ty * 64})
                    
                    for pid, p in self.players.items():
                        px, py = int((p['x'] + 32) // 64), int((p['y'] + 32) // 64)
                        if px == tx and py == ty and p['alive']:
                            p['alive'] = False
                            await self.broadcast({'type': 'player_killed', 'id': pid})
                            asyncio.create_task(self.check_win_condition())

                    for other_b in self.bombs[:]:
                        obx, oby = int((other_b['x']+32)//64), int((other_b['y']+32)//64)
                        if obx == tx and oby == ty:
                            self.bombs.remove(other_b)
                            await self.broadcast({'type': 'bombs_update', 'bombs': self.bombs})
                            asyncio.create_task(self.bomb_logic(other_b))

                    if cell == WALL_SOFT:
                        drop = FLOOR
                        roll = random.random()
                        if roll < 0.20: drop = ITEM_FIRE
                        elif roll < 0.35: drop = ITEM_SPEED
                        elif roll < 0.45: drop = ITEM_AMMO
                        elif roll < 0.50: drop = ITEM_KICK
                        elif roll < 0.55: drop = ITEM_GHOST
                        self.map[ty][tx] = drop
                        await self.broadcast({'type': 'map_update', 'x': tx, 'y': ty, 'val': drop})
                        break
        await self.broadcast({'type': 'explosion', 'cells': explosion_cells})

    async def check_win_condition(self):
        if not self.game_started or len(self.players) < 2: return
        alive = [p for p in self.players.values() if p['alive']]
        if len(alive) <= 1:
            winner = alive[0] if alive else None
            await self.broadcast({'type': 'game_over', 'winner_id': winner['id'] if winner else None, 'winner_name': winner['nickname'] if winner else 'Nadie'})
            await asyncio.sleep(5)
            self.game_started = False
            self.regenerate_map(15) 
            self.bombs = []
            idx = 0
            for pid, p in self.players.items():
                pos = self.get_start_pos(idx)
                p.update({"alive": True, "x": pos[0], "y": pos[1], "range": 1, "max_bombs": 1, "ghost": False, "kick": False})
                idx += 1
            await self.broadcast({"type": "reset_game", "map": self.map, "players": self.players, "grid_size": self.grid_size, "host_id": list(self.players.keys())[0]})

    # --- HANDLER PARA AIOHTTP (Maneja HTTP y WS) ---
    async def handle_request(self, request):
        # 1. Si es Health Check (HTTP), responde OK
        if request.headers.get('Upgrade', '').lower() != 'websocket':
            return web.Response(text="OK - Bomberman Server Running")

        # 2. Si es WebSocket, inicia la conexión
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Si el juego ya empezó, rechazar conexión
        if self.game_started:
            await ws.send_json({
                "type": "error", "message": "⚠️ PARTIDA EN CURSO ⚠️\nEspera a que termine la ronda."
            })
            await ws.close()
            return ws

        # Inicialización de jugador
        pid = str(uuid.uuid4())[:8]
        self.clients[ws] = pid
        pos = self.get_start_pos(len(self.players))
        colors = ["#ef4444", "#3b82f6", "#22c55e", "#eab308", "#a855f7", "#ec4899"]
        
        self.players[pid] = {
            "id": pid, "nickname": f"Player {pid[:4]}",
            "x": pos[0], "y": pos[1], "color": colors[len(self.players) % len(colors)],
            "alive": True, "range": 1, "max_bombs": 1, "ghost": False, "kick": False
        }
        
        await ws.send_json({
            "type": "init", "id": pid, "players": self.players, 
            "map": self.map, "game_started": self.game_started, 
            "host_id": list(self.players.keys())[0], "grid_size": self.grid_size
        })
        await self.broadcast({'type': 'player_joined', 'player': self.players[pid]}, exclude=ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    p = self.players.get(pid)
                    if not p: continue

                    if data["type"] == "set_nickname":
                        p["nickname"] = data["name"][:12]
                        await self.broadcast({"type": "update_stats", "id": pid, "player": p})

                    elif data["type"] == "start_trigger":
                        count = len(self.players)
                        new_size = 13 if count <= 2 else (19 if count >= 5 else 15)
                        self.regenerate_map(new_size)
                        self.game_started = True
                        self.bombs = []
                        idx = 0
                        for pl in self.players.values():
                            pl['x'], pl['y'] = self.get_start_pos(idx)
                            idx += 1
                        await self.broadcast({"type": "reset_game", "map": self.map, "players": self.players, "grid_size": new_size, "host_id": pid})
                        await self.broadcast({"type": "start_game_signal"})

                    elif data["type"] == "move" and p['alive']:
                        gx, gy = int((data['x'] + 32) // 64), int((data['y'] + 32) // 64)
                        if 0 <= gy < self.grid_size and 0 <= gx < self.grid_size:
                            cell = self.map[gy][gx]
                            if cell >= 3:
                                kind = 'UNKNOWN'
                                if cell == ITEM_FIRE: p['range'] += 1; kind = 'FIRE'
                                elif cell == ITEM_SPEED: kind = 'SPEED'
                                elif cell == ITEM_GHOST: p['ghost'] = True; kind = 'GHOST'
                                elif cell == ITEM_KICK: p['kick'] = True; kind = 'KICK'
                                elif cell == ITEM_AMMO: p['max_bombs'] += 1; kind = 'AMMO'
                                self.map[gy][gx] = FLOOR
                                await self.broadcast({'type': 'map_update', 'x': gx, 'y': gy, 'val': FLOOR})
                                await self.broadcast({'type': 'powerup', 'id': pid, 'kind': kind})

                        if p['kick']:
                            for b in self.bombs:
                                dist = ((p['x'] - b['x'])**2 + (p['y'] - b['y'])**2)**0.5
                                if dist < 40: 
                                    dx = b['x'] - p['x']; dy = b['y'] - p['y']
                                    if abs(dx) > abs(dy): b['vx'] = 1 if dx > 0 else -1; b['vy'] = 0
                                    else: b['vx'] = 0; b['vy'] = 1 if dy > 0 else -1

                        p["x"], p["y"] = data["x"], data["y"]
                        await self.broadcast({"type": "update", "id": pid, "x": p["x"], "y": p["y"]}, exclude=ws)

                    elif data["type"] == "bomb" and p['alive']:
                        active_bombs = sum(1 for b in self.bombs if b['owner'] == pid)
                        if active_bombs < p['max_bombs']:
                            bx, by = data['x'], data['y']
                            occupied = any(b['x'] == bx and b['y'] == by for b in self.bombs)
                            if not occupied:
                                new_bomb = {'x': bx, 'y': by, 'range': p['range'], 'owner': pid, 'vx': 0, 'vy': 0}
                                self.bombs.append(new_bomb)
                                asyncio.create_task(self.bomb_logic(new_bomb))
                                await self.broadcast({"type": "bombs_update", "bombs": self.bombs})
                
                elif msg.type == WSMsgType.ERROR:
                    print('ws connection closed with exception %s', ws.exception())

        finally:
            await self.handle_disconnect(ws)
        
        return ws

async def main():
    PORT = int(os.environ.get("PORT", 10000))
    print(f"🔥 Servidor V15 (AIOHTTP Pro) - Puerto {PORT}")
    
    server = BombermanServer()
    app = web.Application()
    # Ruta única que maneja tanto HTTP (Health Check) como WS (Juego)
    app.add_routes([web.get('/', server.handle_request), web.get('/health', server.handle_request)])
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    print("🚀 Servidor en línea (Inmune a Health Checks)")
    await asyncio.Future() # Mantener vivo

if __name__ == "__main__":
    asyncio.run(main())
