local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local member, window, lease = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
local wait, reason = 0, 0
for i = 1, 6 do
    local rate = i <= 2
    local cutoff = rate and now - window or now
    for _, n in ipairs({i, i + 6}) do
        redis.call('ZREMRANGEBYSCORE', KEYS[n], '-inf', cutoff)
    end
    local capacity = tonumber(ARGV[i + 3])
    if redis.call('ZSCORE', KEYS[i], member) or redis.call('ZCARD', KEYS[i]) >= capacity then
        local first = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
        local remaining = tonumber(first[2]) - now
        if rate then remaining = remaining + window else remaining = math.min(remaining, 1000) end
        if remaining >= wait then wait, reason = math.max(1, remaining), i end
    end
end
-- 服务分类仅记录实际使用量，不能因另一类曾经请求过就闲置仍可用的容量。
local metric = KEYS[13]
if wait > 0 then
    redis.call('HINCRBY', metric, 'local_denied:' .. tostring(reason) .. ':' .. ARGV[10], 1)
    redis.call('EXPIRE', metric, 172800)
    return {0, math.ceil(wait), reason}
end
redis.call('HINCRBY', metric, 'granted:' .. ARGV[10], 1)
redis.call('EXPIRE', metric, 172800)
for i = 1, 6 do
    local rate = i <= 2
    local score = rate and now or now + lease
    for _, n in ipairs({i, i + 6}) do
        redis.call('ZADD', KEYS[n], score, member)
        local latest = redis.call('ZRANGE', KEYS[n], -1, -1, 'WITHSCORES')
        local ttl = rate and window + 1000 or math.ceil(tonumber(latest[2]) - now + 1000)
        redis.call('PEXPIRE', KEYS[n], ttl)
    end
end
return {1, 0, 0}
