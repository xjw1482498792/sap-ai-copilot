-- SQLite
-- 我想创建一个表，并存入测试数据
-- 创建成绩测试表
CREATE TABLE test_scores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    score     INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    test_time TEXT NOT NULL
);

-- 插入测试数据
DELETE FROM test_scores;
INSERT INTO test_scores (name, score, test_time)
VALUES
    ('张1', 50, '2025-07-07'),
    ('张2', 92, '2025-07-07'),
    ('张3', 93, '2025-07-07'),
    ('张4', 94, '2025-07-07'),
    ('张5', 88, '2025-07-07'),
    ('张1', 90, '2025-08-08'),
    ('张2', 92, '2025-08-08'),
    ('张3', 93, '2025-08-08'),
    ('张4', 94, '2025-08-08'),
    ('张5', 88, '2025-08-08');    

-- 修改某条
UPDATE test_scores SET score = 59 where score = 60

-- 查看数据
SELECT *
FROM test_scores

-- 平均分数
SELECT test_time,
       avg(score)
FROM test_scores
GROUP BY test_time;

-- 不到平均分数的人
SELECT DISTINCT test_scores.name
FROM test_scores
INNER JOIN 
    (
    SELECT test_time,
        avg(score) as avg_score
    FROM test_scores
    GROUP BY test_time
    ) as avg_scores
ON test_scores.test_time = avg_scores.test_time
WHERE test_scores.score < avg_scores.avg_score


-- 不及格的人
SELECT DISTINCT name
FROM test_scores
WHERE score < 60;

WITH avg_column AS (
    SELECT name,
           score,
           test_time,
           AVG(score) OVER (
            
           )

)
--每次必须及格
--每次考试高于平均分
--个人累计总成绩排名前三
SELECT name,
       sum(score) AS score_all
FROM test_scores
WHERE 
-- name not in 
--     (
--     SELECT DISTINCT name
--     FROM test_scores
--     WHERE score < 60
--     ) 
-- AND 
name not IN
    (
    SELECT DISTINCT test_scores.name
    FROM test_scores
    INNER JOIN 
        (
        SELECT test_time,
            avg(score) as avg_score
        FROM test_scores
        GROUP BY test_time
        ) as avg_scores
    ON test_scores.test_time = avg_scores.test_time
    WHERE test_scores.score < avg_scores.avg_score
    )
AND test_time LIKE "2025%"
GROUP BY name
HAVING MIN(score) >= 60    
ORDER BY score_all DESC  
LIMIT 3   ;