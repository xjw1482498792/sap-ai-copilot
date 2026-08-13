#2列表原地奇偶排序（奇数左边，偶数右边）



def sort_list(nums)->list:
    left = 0
    right = len(nums) - 1
    while left < right:
        if nums[left] % 2 == 1:
            left += 1
            continue
        if nums[right] % 2 == 0:
            right -= 1
            continue
        #left不是奇数，right不是偶数
        nums[left], nums[right] = nums[right], nums[left]    
        left += 1
        right -= 1

    return nums  

#1手写计时装饰器    
# import time
# from functools import wraps

# def timer(func):
# 	@wraps(func)
# 	def wrapper(*args, **kwargs):
# 		start = time.perf_counter()

# 		try:
# 			return func(*args, **kwargs)
# 		finally:
# 			elapsed = time.perf_counter() - start
# 			print(f"{func.__name__} 耗时：{elapsed:.4f} 秒")

# 	return wrapper

# @timer
# def query_data(delay):
# 	time.sleep(delay)
# 	return "执行成功"

# if __name__ == "__main__":
# 	result = query_data(2)
# 	print(result)	

import time

def deco_func(func):
	def wrapper(*args, **kwargs):
		begin = time.time()
		try:
			return func(*args, **kwargs)
		finally:		
			end = time.time()
			print(f'执行时间为{end - begin}秒')
	return wrapper 

@deco_func
def my_func(seconds):
	time.sleep(seconds)
	return f'my_func is running'

#3. 反转字符串/单词反转
def reverse_str(str1: str):
	#前两个值表示左闭右开区间，第三个值表示步长（带方向）
	# tmp = "abcdefg"
	# print(tmp[5:0:-2])	
	#反转整个字符串
	res = str1[::-1]
	#只反转单词内容
	res = " ".join(word[::-1] for word in str1.split(" "))
	#只反转单词顺序
	# list1 = str1.split(" ")
	# list1.reverse()
	# res = " ".join(word for word in list1)
	res = " ".join(word for word in str1.split(" ")[::-1])
	return res

#4. 爬楼梯DP基础题
def climb(n: int):

    if n == 1:
        return 1
    if n == 2:
        return 2    


    return climb(n - 1) + climb(n - 2)

print(climb(3))
if __name__ == "__main__":
	# print(sort_list([1,2,3,4,5,6]))
	# print(my_func(2))
	# print(reverse_str("hello world"))
      print(climb(3))
	