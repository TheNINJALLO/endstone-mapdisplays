from endstone import Server

def get_map_methods():
    methods = [m for m in dir(Server) if 'map' in m.lower()]
    print(methods)

get_map_methods()
