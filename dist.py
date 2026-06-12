a = 0x5dcf072e1a2c210e
b = 0x2fb846ded288282e
c = 0x0fbef01e604928e1
d = 0x2dadeaee90af780f
def dist(x, y): return bin(x ^ y).count('1')
print(f'21129-21240: {dist(a, b)}')
print(f'21129-21193: {dist(a, c)}')
print(f'21129-21222: {dist(a, d)}')
