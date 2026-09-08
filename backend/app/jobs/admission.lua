local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local member = ARGV[1]
local window = tonumber(ARGV[2])
local lease = tonumber(ARGV[3])
local wait = 0
for i = 1, 6 do
    local cutoff = now
    if i <= 2 then cutoff = now - window end
    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
    local capacity = tonumber(ARGV[i + 3])
    local current = redis.call('ZSCORE', KEYS[i], member)
    if current then
        local remaining = tonumber(current) - now
        if i <= 2 then remaining = remaining + window end
        wait = math.max(wait, remaining)
    end
    if redis.call('ZCARD', KEYS[i]) >= capacity then
        local first = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        local remaining = tonumber(first[2]) - now
        if i <= 2 then remaining = remaining + window end
        wait = math.max(wait, remaining)
    end
end
if wait > 0 then return {0, math.ceil(wait)} end
for i = 1, 6 do
    local score = now + lease
    local ttl = lease + 1000
    if i <= 2 then score = now; ttl = window + 1000 end
    redis.call('ZADD', KEYS[i], score, member)
    if i > 2 then
        local latest = redis.call('ZRANGE', KEYS[i], -1, -1, 'WITHSCORES')
        ttl = math.ceil(tonumber(latest[2]) - now + 1000)
    end
    redis.call('PEXPIRE', KEYS[i], ttl)
end
return {1, 0}
