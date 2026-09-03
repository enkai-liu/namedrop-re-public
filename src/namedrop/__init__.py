"""namedrop-re: an interoperable re-implementation of Apple NameDrop.

    nfc_ecp  -> build the NFC ECP bump frame (pure logic; the Proxmark3 emits its own)
    contact  -> build/parse the vCard payload

Both are import-safe on any machine. The live path is the Proxmark3 standalone mode in
firmware/ plus scripts/mdns-advertise.py and scripts/asquic-receiver.py -- see the README.
"""

__version__ = "0.0.1"
