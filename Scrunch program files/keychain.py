"""Store provider secrets through macOS Security.framework, never CLI arguments."""
import ctypes as C
import sys

def operation(account, secret=None):
    if sys.platform != 'darwin': raise ValueError('Persistent secrets currently require macOS Keychain.')
    cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec=C.CDLL('/System/Library/Frameworks/Security.framework/Security')
    ptr=C.c_void_p
    def bind(lib,name,args,result):
        fn=getattr(lib,name); fn.argtypes=args; fn.restype=result; return fn
    string=bind(cf,'CFStringCreateWithCString',[ptr,C.c_char_p,C.c_uint32],ptr)
    data=bind(cf,'CFDataCreate',[ptr,C.c_char_p,C.c_long],ptr)
    dictionary=bind(cf,'CFDictionaryCreate',[ptr,ptr,ptr,C.c_long,ptr,ptr],ptr)
    release=bind(cf,'CFRelease',[ptr],None)
    const=lambda name:ptr.in_dll(sec,name).value
    owned=[]
    def s(v):
        p=string(None,v.encode(),0x08000100); owned.append(p); return p
    def d(v):
        b=v.encode(); p=data(None,b,len(b)); owned.append(p); return p
    def mapping(pairs):
        keys=(ptr*len(pairs))(*(x[0] for x in pairs)); vals=(ptr*len(pairs))(*(x[1] for x in pairs))
        p=dictionary(None,keys,vals,len(pairs),None,None); owned.append(p); return p
    pairs=[(const('kSecClass'),const('kSecClassGenericPassword')),
           (const('kSecAttrService'),s('org.scrunch.midi-engineer')),
           (const('kSecAttrAccount'),s(account))]
    try:
        query=mapping(pairs)
        if secret is not None:
            val=d(secret); attrs=mapping([(const('kSecValueData'),val)])
            status=bind(sec,'SecItemUpdate',[ptr,ptr],C.c_int)(query,attrs)
            if status == -25300:
                status=bind(sec,'SecItemAdd',[ptr,ptr],C.c_int)(mapping(pairs+[(const('kSecValueData'),val)]),None)
            if status: raise ValueError(f'Keychain save failed ({status}). No secret was saved to project files.')
            return True
        yes=ptr.in_dll(cf,'kCFBooleanTrue').value
        query=mapping(pairs+[(const('kSecReturnData'),yes)])
        output=ptr()
        status=bind(sec,'SecItemCopyMatching',[ptr,C.POINTER(ptr)],C.c_int)(query,C.byref(output))
        if status == -25300: return None
        if status: raise ValueError(f'Keychain access failed ({status}).')
        try:
            size=bind(cf,'CFDataGetLength',[ptr],C.c_long)(output)
            address=bind(cf,'CFDataGetBytePtr',[ptr],ptr)(output)
            return C.string_at(address,size).decode()
        finally: release(output)
    finally:
        for p in reversed(owned): release(p)
