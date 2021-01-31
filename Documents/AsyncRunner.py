# ************************************************************************
# *   Copyright (c) Stefan Troeger (stefantroeger@gmx.net) 2019          *
# *                                                                      *
# *   This library is free software; you can redistribute it and/or      *
# *   modify it under the terms of the GNU Library General Public        *
# *   License as published by the Free Software Foundation; either       *
# *   version 2 of the License, or (at your option) any later version.   *
# *                                                                      *
# *   This library  is distributed in the hope that it will be useful,   *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of     *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the      *
# *   GNU Library General Public License for more details.               *
# *                                                                      *
# *   You should have received a copy of the GNU Library General Public  *
# *   License along with this library; see the file COPYING.LIB. If not, *
# *   write to the Free Software Foundation, Inc., 59 Temple Place,      *
# *   Suite 330, Boston, MA  02111-1307, USA                             *
# ************************************************************************

import asyncio

class TaskContext():
    def __init__(self, tasks):
        self.tasks = tasks

    async def __aenter__(self):
        #wait till all setup tasks are finished
        if len(self.tasks) > 0:
            await asyncio.wait(self.tasks)

    async def __aexit__(self, exc_type, exc, tb):
        pass

#Syncers are classs to achieve syncronisation with multiple runners and an outside process.
#They are intended to be used with Asyncrunners "syncronize" methods, which adds their "excecute" method
#to the current task list
    
class AcknowledgeSyncer():
    #Allows to wait till num synced runners have excecuted the syncer. 
    #waitAllAchnowledge blocks till this happens. The runners are not blocked, 
    #they directly execute their tasks after the syncer.
    
    def __init__(self, num):
        self.count = num
        self.event = asyncio.Event()

    async def excecute(self):
        self.count -= 1
        if self.count <= 0:
            self.event.set()

    async def waitAllAchnowledge(self, timeout = 60):
        await asyncio.wait_for(self.event.wait(), timeout)
        

class BlockSyncer():
    #Blocks all synced runners until the restart method is called
    
    def __init__(self):
        self.event = asyncio.Event()

    async def excecute(self):
        await self.event.wait()

    def restart(self):
        self.event.set()
        
    async def asyncRestart(self):
        self.restart()


class AcknowledgeBlockSyncer():
    #Allows to wait for num runners to execute and than blocks then till restart is called
       
    def __init__(self, num):
        self.Acknowledge = AcknowledgeSyncer(num)
        self.Block = BlockSyncer()
       
    async def excecute(self):
        await self.Acknowledge.excecute()
        await self.Block.excecute()
            
    async def wait(self):
        await self.Acknowledge.waitAllAchnowledge()
        
    def restart(self):
        self.Block.restart()
            

class DocumentRunner():
    #Generates sender and receiver DocumentBatchedOrderedRunner for a whole document where all actions on all 
    #individual runners are executed in order
    
    __sender   = {}
    __receiver = {}
    
    @classmethod
    def getSenderRunner(cls, docId, logger):
        if not docId in DocumentRunner.__sender:
            DocumentRunner.__sender[docId] = OrderedRunner(logger)
        
        return DocumentBatchedOrderedRunner(DocumentRunner.__sender[docId])
    
    @classmethod
    def getReceiverRunner(cls, docId, logger):
        if not docId in DocumentRunner.__receiver:
            DocumentRunner.__receiver[docId] = OrderedRunner(logger)
        
        return DocumentBatchedOrderedRunner(DocumentRunner.__receiver[docId])
   

    
class OrderedRunner():
    #AsyncRunner which runs task in order
   
    #runns all tasks syncronous
    def __init__(self, logger):
        
        self.__logger        = logger
        self.__tasks         = []
        self.__syncEvent     = asyncio.Event() 
        self.__finishEvent   = asyncio.Event()
        self.__current       = ""
        
        self.__maintask = asyncio.ensure_future(self.__run())

   
    async def waitTillCloseout(self, timeout = 10):
        try:
            await asyncio.wait_for(self.__finishEvent.wait(), timeout)
            
        except asyncio.TimeoutError as e:
            remaining = self.queued()
            self.__logger.error(f"Runner closeout timed out while working ({not self.__maintask.done()}) on {self.__current}. Remaining: \n{remaining}")     
         

    async def close(self):
        await self.waitTillCloseout()
        try:
            self.__shutdown = True
            if not self.__maintask.cancelled():
                self.__maintask.cancel()
                await self.__maintask
        except asyncio.CancelledError:
            pass
        
        self.__finishEvent.set()       

    async def __run(self):
        
        self.__finishEvent.set()
        while True:
            try:
                await self.__syncEvent.wait()
                self.__finishEvent.clear()
                        
                #work the tasks syncronous
                task = self.__tasks.pop(0)
                while task:
                    self.__current = task[0].__name__
                    await task[0](*task[1])
                    if self.__tasks:
                        task = self.__tasks.pop(0)
                    else:
                        task = None
                    
                self.__finishEvent.set()
                self.__syncEvent.clear()
                
            except Exception as e:
                self.__logger.error(f"{e}")
                
        if not self.__shutdown:
            self.__logger.error(f"Main loop of sync runner closed unexpectedly: {e}")
        
           
    def run(self, fnc, *args):
        
        self.__tasks.append((fnc, args))
        self.__syncEvent.set()
        
        
    def queued(self):
        #returns the names of all currently queued tasks
        return [task[0].__name__ for task in self.__tasks]
    
    
    def sync(self, syncer):
        self.run(syncer.excecute)


class BatchedOrderedRunner():
    #batched ordered execution of tasks
    #Normally run received a function object of an async function and its arguments.
    #The functions are than processed in order one by one (each one awaited). If functions can be batched
    #together, this can be done in the following way:
    #1. register batch handler. This is a async function which is called after all batchable functions are executed
    #2. run functions that have a batchhandler assigned. Those functions must not be awaitables, but default functions.

    #runns all tasks syncronous and batches tasks together if possible
    def __init__(self, logger):

        self.__logger        = logger
        self.__tasks         = []
        self.__syncEvent     = asyncio.Event() 
        self.__finishEvent   = asyncio.Event()
        self.__batchHandler  = {}
        self.__current       = ""
        self.__shutdown      = False

        self.__maintask = asyncio.ensure_future(self.__run())


    def registerBatchHandler(self, fncName, batchFnc):        
        self.__batchHandler[fncName] = batchFnc;


    async def waitTillCloseout(self, timeout = 10):     
        try:
            await asyncio.wait_for(self.__finishEvent.wait(), timeout)
            
        except asyncio.TimeoutError as e:
            remaining = self.queued()
            self.__logger.error(f"Runner closeout timed out while working ({not self.__maintask.done()}) on {self.__current}. Remaining: \n{remaining}")     


    async def close(self):
               
        await self.waitTillCloseout()
        try:
            self.__shutdown = True
            if not self.__maintask.cancelled():
                self.__maintask.cancel()
                await self.__maintask
        except asyncio.CancelledError:
            pass
        
        self.__finishEvent.set()
        

    async def __run(self):
                  
        #initially we have no work
        self.__finishEvent.set()
            
        while True:
            
            try:
                #wait till new tasks are given.
                await self.__syncEvent.wait()
                
                #inform that we are working
                self.__finishEvent.clear()            
                    
                #work the tasks in order
                task = self.__tasks.pop(0)
                while task:
                    
                    self.__current = task[0].__name__
                    
                    #check if we can batch tasks
                    if self.__current in self.__batchHandler:
                        
                        #execute all batchable functions of this type
                        batchtask = task
                        while batchtask and batchtask[0].__name__ == self.__current:
                            
                            batchtask[0](*batchtask[1])
                            if self.__tasks:
                                batchtask = self.__tasks.pop(0)
                            else:
                                batchtask = None
                                break
                        
                        #rund the batch handler
                        await self.__batchHandler[self.__current]()
                        
                        #reset the outer loop
                        task = batchtask
                        continue
                    
                    else:
                        #not batchable, normal operation
                        await task[0](*task[1])
                        if self.__tasks:
                            task = self.__tasks.pop(0)
                        else:
                            task = None
                
                self.__current = ""
                self.__finishEvent.set()
                self.__syncEvent.clear()

                            
            except Exception as e:
                self.logger.error(f"{e}")
                
        if not self.__shutdown:            
            self.logger.error(f"Unexpected shutdown in BatchedOrderedRunner: {e}")
        
           
    def run(self, fnc, *args):
        
        self.__tasks.append((fnc, args))
        self.__syncEvent.set()
        
    def queued(self):
        #returns the names of all currently queued tasks
        return [task[0].__name__ for task in self.__tasks]
        
    def sync(self, syncer):
        self.run(syncer.excecute)
        

class DocumentBatchedOrderedRunner():
    #A Async runner that syncronizes over the whole document, and has the same API as the BatchedOrderedRunner to be 
    #compatible replacement
    
    def __init__(self, runner):
        self.__docRunner = runner
        self.__batchHandler = {}
        
        
    def registerBatchHandler(self, fncName, batchFnc):        
        self.__batchHandler[fncName] = batchFnc;
        
    
    def run(self, fnc, *args):
        
        #check if this function needs to be handled by batch function, and 
        #build a wrapper if so
        if fnc.__name__ in self.__batchHandler:
            
            handler = self.__batchHandler[fnc.__name__ ]
            
            async def wrapper(*args):
                fnc(*args)
                await handler()
                
            self.__docRunner.run(wrapper, *args)
            
        else:
            self.__docRunner.run(fnc, *args)
                
                
    def queued(self):
        #returns the names of all currently queued tasks
        return self.__docRunner.queued()
        
    def sync(self, syncer):
        #syncronisation: provide a syncer. The runner calls done() when all currently 
        #available work is done and afterwards wait for the restart till new things are processed
        return self.__docRunner.sync(syncer)
                
                
    async def waitTillCloseout(self, timeout = 10):
        #Returns when all active tasks are finished. Also waits for tasks added after the call to this function
        return await self.__docRunner.waitTillCloseout(timeout)


    async def close(self):
        return await self.__docRunner.close()
           

