-- Only tenant turn metadata. Celery/outbox own all durable business work.
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local mode, tenant, owner = ARGV[1], ARGV[2], ARGV[3]
local wait_ms, turn_ms = tonumber(ARGV[4]), tonumber(ARGV[5])
local token = tenant .. ':' .. owner
local function remove(candidate)
    for _, i in ipairs({1, 4, 5}) do redis.call('ZREM', KEYS[i], candidate) end
end
if mode == 'finish' then
    if redis.call('GET', KEYS[2]) ~= token then return 0 end
    redis.call('DEL', KEYS[2])
    remove(tenant)
    if ARGV[6] ~= '1' then
        redis.call('ZADD', KEYS[4], now, tenant)
        redis.call('ZADD', KEYS[5], now + tonumber(ARGV[7]), tenant)
    end
    for _, i in ipairs({1, 3, 4, 5}) do
        redis.call('PEXPIRE', KEYS[i], math.max(redis.call('PTTL', KEYS[i]), wait_ms * 2, tonumber(ARGV[7]) + wait_ms))
    end
    return 1
end
-- Each script does bounded cleanup/promotion; no all-tenant ZRANGE scan.
local stale = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now - wait_ms, 'LIMIT', 0, 100)
for _, candidate in ipairs(stale) do remove(candidate) end
local due = redis.call('ZRANGEBYSCORE', KEYS[5], '-inf', now, 'LIMIT', 0, 100)
for _, candidate in ipairs(due) do
    local seen = tonumber(redis.call('ZSCORE', KEYS[4], candidate) or '0')
    if seen > now - wait_ms then
        redis.call('ZADD', KEYS[1], redis.call('INCR', KEYS[3]), candidate)
        redis.call('ZREM', KEYS[5], candidate)
    else remove(candidate) end
end
if not redis.call('ZSCORE', KEYS[1], tenant) and not redis.call('ZSCORE', KEYS[5], tenant) then
    redis.call('ZADD', KEYS[1], redis.call('INCR', KEYS[3]), tenant)
end
redis.call('ZADD', KEYS[4], now, tenant)
for _, i in ipairs({1, 3, 4, 5}) do redis.call('PEXPIRE', KEYS[i], math.max(redis.call('PTTL', KEYS[i]), wait_ms * 2)) end
-- A delayed tenant remains in the due set, never at the ready queue head.
local head = redis.call('ZRANGE', KEYS[1], 0, 0)[1]
if head ~= tenant then return 0 end
if not redis.call('SET', KEYS[2], token, 'NX', 'PX', turn_ms) then return 0 end
return 1
