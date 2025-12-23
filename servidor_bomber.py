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

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.clients = {} 
        self.players = {} 
        self.game_started = False
        self.grid_size = 15
        self.map = []
        self.bombs = [] 
        self.regenerate_map(15)
        self.physics_task = asyncio.create_task(self.physics_loop())

    def stop(self):
        if self.physics_task:
            self.physics_task.cancel()

    # --- NUEVO: Calcula las coordenadas lógicas (Grid) de los 8 spawns ---
    def get_spawn_grid_coords(self, size):
        mid = size // 2
        # Lista de 8 posiciones (Columna X, Fila Y)
        # Orden: Esquinas primero, luego centros (para balancear si son 5 o 6)
        spawns = [
            (1, 1),                 # 1. Top-Izquierda
            (size-2, size-2),       # 2. Bottom-Derecha
            (size-2, 1),            # 3. Top-Derecha
            (1, size-2),            # 4. Bottom-Izquierda
            (mid, 1),               # 5. Top-Centro
            (mid, size-2),          # 6. Bottom-Centro
            (1, mid),               # 7. Izquierda-Centro
            (size-2, mid)           # 8. Derecha-Centro
        ]
        return spawns

    def regenerate_map(self, size):
        self.grid_size = size
        self.map = [[0 for _ in range(size)] for _ in range(size)]
        
        # 1. Obtener puntos de spawn para protegerlos
        spawn_points = self.get_spawn_grid_coords(size)
        
        # Crear set de zonas prohibidas (Spawn + sus vecinos directos)
        safe_zone = set()
        for (sx, sy) in spawn_points:
            safe_zone.add((sx, sy))
            safe_zone.add((sx+1, sy))
            safe_zone.add((sx-1, sy))
            safe_zone.add((sx, sy+1))
            safe_zone.add((sx, sy-1))

        # 2. Generación del Mapa
        for y in range(size):
            for x in range(size):
                # Bordes sólidos
                if x == 0 or x == size-1 or y == 0 or y == size-1:
                    self.map[y][x] = WALL_HARD
                # Pilares sólidos internos
                elif x % 2 == 0 and y % 2 == 0:
                    self.map[y][x] = WALL_HARD
                else:
                    # Si es zona segura, FORZAR SUELO
                    if (x, y) in safe_zone:
                        self.map[y][x] = FLOOR
                    # Si no, azar para bloque blando
                    elif random.random() < 0.50: # Subí un poco la densidad para mapas grandes
                        self.map[y][x] = WALL_SOFT
                    else:
                        self.map[y][x] = FLOOR

    # --- NUEVO: Obtiene posición en PIXELES basada en los spawns ---
    def get_start_pos(self, index):
        # Usamos modulo 8 para ciclar entre las 8 posiciones
        spawns = self.get_spawn_grid_coords(self.grid_size)
        idx = index % len(spawns)
        gx, gy = spawns[idx]
        return (gx * 64, gy * 64)

    async def broadcast(self, message, exclude=None):
        if not self.clients: return
        data = json.dumps(message)
        disconnected = []
        for ws in list(self.clients.keys()):
            if ws != exclude:
                try: 
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

    async def disable_ghost_later(self, pid, duration):
        await asyncio.sleep(duration)
        if pid in self.players and self.players[pid]['alive']:
            self.players[pid]['ghost'] = False
            await self.broadcast({
                'type': 'powerup_expired', 
                'id': pid, 
                'kind': 'GHOST'
            })

    async def physics_loop(self):
        try:
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
                        
                        collision = False
                        
                        if 0 <= gx < self.grid_size and 0 <= gy < self.grid_size:
                            if self.map[gy][gx] != FLOOR:
                                collision = True
                        else:
                            collision = True 

                        if not collision:
                            for other in self.bombs:
                                if other is b: continue 
                                dist = ((new_x - other['x'])**2 + (new_y - other['y'])**2)**0.5
                                if dist < 50: 
                                    collision = True
                                    break

                        if collision:
                            b['vx'] = 0; b['vy'] = 0
                            b['x'] = round(b['x'] / 64) * 64
                            b['y'] = round(b['y'] / 64) * 64
                            moved = True
                        else:
                            b['x'] = new_x
                            b['y'] = new_y
                            moved = True
                
                if moved:
                    await self.broadcast({'type': 'bombs_update', 'bombs': self.bombs})
        except asyncio.CancelledError:
            pass

    async def bomb_timer(self, bomb_obj):
        await asyncio.sleep(3.0)
        await self.detonate(bomb_obj)

    async def detonate(self, bomb_obj):
        if bomb_obj not in self.bombs:
            return 

        self.bombs.remove(bomb_obj)
        await self.broadcast({'type': 'bombs_update', 'bombs': self.bombs})
        
        bx, by = bomb_obj['x'], bomb_obj['y']
        brange = bomb_obj['range']
        gx, gy = int((bx+32) // 64), int((by+32) // 64)
        explosion_cells = []
        
        explosion_cells.append({'x': gx * 64, 'y': gy * 64})
        
        directions = [(0,-1), (0,1), (-1,0), (1,0)] 

        for dx, dy in directions:
            for i in range(brange):
                dist = i + 1
                tx, ty = gx + (dx * dist), gy + (dy * dist)
                
                if not (0 <= tx < self.grid_size and 0 <= ty < self.grid_size):
                    break
                
                cell = self.map[ty][tx]
                
                if cell == WALL_HARD: 
                    break
                
                explosion_cells.append({'x': tx * 64, 'y': ty * 64})

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

                for other_b in self.bombs[:]: 
                    obx, oby = int((other_b['x']+32)//64), int((other_b['y']+32)//64)
                    if obx == tx and oby == ty:
                        asyncio.create_task(self.detonate(other_b))

        for pid, p in self.players.items():
            if not p['alive']: continue
            px, py = int((p['x'] + 32) // 64), int((p['y'] + 32) // 64)
            
            hit = False
            for c in explosion_cells:
                cx, cy = int(c['x'] // 64), int(c['y'] // 64)
                if px == cx and py == cy:
                    hit = True
                    break
            
            if hit:
                p['alive'] = False
                await self.broadcast({'type': 'player_killed', 'id': pid})
                asyncio.create_task(self.check_win_condition())

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


active_rooms = {} 

async def handle_request(request):
    if request.headers.get('Upgrade', '').lower() != 'websocket':
        return web.Response(text=f"BomberServer OK - {len(active_rooms)} Rooms Active")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        current_room = None
        msg = await ws.receive()
        if msg.type == WSMsgType.TEXT:
            login_data = json.loads(msg.data)
            
            if login_data['action'] == 'CREATE':
                room_id = str(random.randint(1000, 9999))
                while room_id in active_rooms:
                    room_id = str(random.randint(1000, 9999))
                current_room = GameRoom(room_id)
                active_rooms[room_id] = current_room
            
            elif login_data['action'] == 'JOIN':
                room_id = login_data.get('code')
                if room_id in active_rooms:
                    current_room = active_rooms[room_id]
                else:
                    await ws.send_json({"type": "error", "message": "❌ Código de sala inválido"})
                    await ws.close()
                    return ws

            if current_room:
                if current_room.game_started:
                     await ws.send_json({"type": "error", "message": "⚠️ PARTIDA EN CURSO ⚠️"})
                     await ws.close()
                     return ws

                pid = str(uuid.uuid4())[:8]
                current_room.clients[ws] = pid
                
                pos = current_room.get_start_pos(len(current_room.players))
                colors = ["#ef4444", "#3b82f6", "#22c55e", "#eab308", "#a855f7", "#ec4899", "#06b6d4", "#f97316"]
                
                current_room.players[pid] = {
                    "id": pid, "nickname": login_data.get('nickname', 'Player')[:12],
                    "x": pos[0], "y": pos[1], "color": colors[len(current_room.players) % len(colors)],
                    "alive": True, "range": 1, "max_bombs": 1, "ghost": False, "kick": False
                }

                await ws.send_json({
                    "type": "init", 
                    "room_code": current_room.room_id,
                    "id": pid, 
                    "players": current_room.players, 
                    "map": current_room.map, 
                    "game_started": current_room.game_started, 
                    "host_id": list(current_room.players.keys())[0], 
                    "grid_size": current_room.grid_size
                })
                await current_room.broadcast({'type': 'player_joined', 'player': current_room.players[pid]}, exclude=ws)

                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        p = current_room.players.get(pid)
                        if not p: continue

                        if data["type"] == "start_trigger":
                            cnt = len(current_room.players)
                            sz = 13 if cnt <= 2 else (19 if cnt >= 5 else 15)
                            current_room.regenerate_map(sz)
                            current_room.game_started = True
                            current_room.bombs = []
                            idx = 0
                            for pl in current_room.players.values():
                                pl['x'], pl['y'] = current_room.get_start_pos(idx)
                                idx += 1
                            await current_room.broadcast({"type": "reset_game", "map": current_room.map, "players": current_room.players, "grid_size": sz, "host_id": pid})
                            await current_room.broadcast({"type": "start_game_signal"})

                        elif data["type"] == "move" and p['alive']:
                            gx, gy = int((data['x'] + 32) // 64), int((data['y'] + 32) // 64)
                            if 0 <= gy < current_room.grid_size and 0 <= gx < current_room.grid_size:
                                cell = current_room.map[gy][gx]
                                if cell >= 3:
                                    kind = 'UNKNOWN'
                                    if cell == ITEM_FIRE: p['range'] += 1; kind = 'FIRE'
                                    elif cell == ITEM_SPEED: kind = 'SPEED'
                                    elif cell == ITEM_GHOST: 
                                        p['ghost'] = True
                                        kind = 'GHOST'
                                        asyncio.create_task(current_room.disable_ghost_later(pid, 15))
                                    elif cell == ITEM_KICK: p['kick'] = True; kind = 'KICK'
                                    elif cell == ITEM_AMMO: p['max_bombs'] += 1; kind = 'AMMO'
                                    current_room.map[gy][gx] = FLOOR
                                    await current_room.broadcast({'type': 'map_update', 'x': gx, 'y': gy, 'val': FLOOR})
                                    await current_room.broadcast({'type': 'powerup', 'id': pid, 'kind': kind})

                            # --- LÓGICA DE KICK DEFINITIVA ---
                            if p['kick']:
                                for b in current_room.bombs:
                                    dist = ((p['x'] - b['x'])**2 + (p['y'] - b['y'])**2)**0.5
                                    if dist < 28: 
                                        continue
                                    if dist < 60: 
                                        input_dx = data.get('dx', 0)
                                        input_dy = data.get('dy', 0)

                                        if input_dx == 0 and input_dy == 0:
                                            continue 

                                        to_bomb_x = b['x'] - p['x']
                                        to_bomb_y = b['y'] - p['y']
                                        dot = (input_dx * to_bomb_x) + (input_dy * to_bomb_y)
                                        
                                        if dot > 0:
                                            b['vx'] = input_dx
                                            b['vy'] = input_dy
                                            if b['vx'] != 0 and b['vy'] != 0:
                                                if abs(to_bomb_x) > abs(to_bomb_y): b['vy'] = 0
                                                else: b['vx'] = 0

                            p["x"], p["y"] = data["x"], data["y"]
                            await current_room.broadcast({"type": "update", "id": pid, "x": p["x"], "y": p["y"]}, exclude=ws)

                        elif data["type"] == "bomb" and p['alive']:
                            active = sum(1 for b in current_room.bombs if b['owner'] == pid)
                            if active < p['max_bombs']:
                                bx, by = data['x'], data['y']
                                occ = any(b['x'] == bx and b['y'] == by for b in current_room.bombs)
                                if not occ:
                                    nb = {'x': bx, 'y': by, 'range': p['range'], 'owner': pid, 'vx': 0, 'vy': 0}
                                    current_room.bombs.append(nb)
                                    asyncio.create_task(current_room.bomb_timer(nb))
                                    await current_room.broadcast({"type": "bombs_update", "bombs": current_room.bombs})

    finally:
        if current_room:
            await current_room.handle_disconnect(ws)
            if not current_room.clients:
                current_room.stop()
                if current_room.room_id in active_rooms:
                    del active_rooms[current_room.room_id]

    return ws

async def main():
    PORT = int(os.environ.get("PORT", 10000))
    print(f"🔥 Servidor V26 (8 Players) - Puerto {PORT}")
    
    app = web.Application()
    app.add_routes([web.get('/', handle_request), web.get('/health', handle_request)])
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
